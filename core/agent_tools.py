"""Custom tools for FuzzyClaw agents to interact with the Django DB and system.

Coordinator-specific tools run host-side in the Celery worker.
Specialist agents run in isolated Docker containers — no in-process fallback.

Async flow:
  1. dispatch_specialist → non-blocking, returns agent_run_id immediately
  2. check_reports → polls Redis Streams (or filesystem fallback) for completions
  3. read_report → reads ONE report from comms/
  4. submit_coordinator_report → final synthesis
"""
import json
import logging
import time

from django.utils import timezone
from langchain_core.tools import tool

from .registry import AgentNotFound, get_agent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Coordinator-specific tools (run host-side in Celery worker)
# ---------------------------------------------------------------------------


@tool
def list_available_agents() -> str:
    """List all active specialist agents registered in the platform.
    Returns JSON with agent names, descriptions, models, skills, and whether
    a pre-built Docker image exists."""
    from .models import AgentImage
    from .registry import get_available_agents

    agents = get_available_agents()
    image_names = set(
        AgentImage.objects.filter(build_error='').values_list('agent_name', flat=True)
    )

    result = []
    for agent in agents:
        entry = {
            'name': agent['name'],
            'description': agent['description'],
            'model': agent['model_choice'],
            'tools': agent['tools'],
            'has_image': agent['name'] in image_names,
        }
        if agent.get('volumes'):
            entry['volumes'] = agent['volumes']
        result.append(entry)
    return json.dumps(result, indent=2)


def make_manage_schedule(briefing):
    """Build a manage_schedule tool bound to a specific briefing.

    The tool cannot mutate schedules for any other briefing — the ID comes
    from the closure, not the model's arguments.
    """
    @tool
    def manage_schedule(schedule_text: str) -> str:
        """Create, update, or remove the current briefing's automatic schedule.

        Pass the schedule as natural language (e.g. "every weekday at 9am",
        "daily at midnight", "every Monday at 2pm EST"). To remove a schedule,
        pass an empty string.

        Args:
            schedule_text: Natural language schedule, or empty string to remove.
        """
        from .scheduling import get_schedule_status, sync_schedule

        briefing.schedule_text = schedule_text.strip()
        briefing.save(update_fields=['schedule_text'])

        try:
            result = sync_schedule(briefing)
        except Exception as e:
            return f"Error scheduling briefing: {e}"

        action = result.get('action', 'none')
        if action == 'error':
            return f"Error: {result.get('error', 'unknown')}"
        if action == 'removed':
            return "Schedule removed."
        if action in ('created', 'updated'):
            return f"Schedule {action}: {result.get('human_readable', schedule_text)}"
        if action in ('paused', 'resumed', 'unchanged'):
            status = get_schedule_status(briefing)
            cron = status['cron'] if status else ''
            return f"Schedule {action}. Cron: {cron}"
        return f"Schedule action: {action}"

    return manage_schedule


def make_dispatch_specialist(run):
    """Build a dispatch_specialist tool bound to the active run.

    The tool cannot dispatch into any other run — the run_id comes from the
    closure, not the model's arguments.
    """
    @tool
    def dispatch_specialist(agent_name: str, task_description: str) -> str:
        """Dispatch a specialist agent in a Docker container. Non-blocking — returns
        immediately with the agent_run_id. Use check_reports to poll for completion,
        then read_report to retrieve the result.

        Args:
            agent_name: The name of the specialist agent to dispatch.
            task_description: What the specialist should do.
        """
        from .containers import start_agent_container
        from .models import AgentRun

        try:
            get_agent(agent_name)
        except AgentNotFound:
            return f"Error: Agent '{agent_name}' not found."

        # Create AgentRun record as 'pending' — only transitions to 'running'
        # after the container actually starts. This prevents phantom records
        # from polluting check_reports when the concurrency limit rejects a dispatch.
        agent_run = AgentRun.objects.create(
            run=run,
            agent_name=agent_name,
            status='pending',
            started_at=timezone.now(),
        )

        try:
            container_id = start_agent_container(
                agent_name=agent_name,
                task_description=task_description,
                agent_run_id=agent_run.id,
                run_id=run.id,
            )

            agent_run.status = 'running'
            agent_run.container_id = container_id
            agent_run.save(update_fields=['status', 'container_id'])

            logger.info(
                "Dispatched specialist '%s' as agent_run_id=%d (container=%s)",
                agent_name, agent_run.id, container_id[:12],
            )
            return f"Dispatched '{agent_name}' as agent_run_id={agent_run.id}"

        except Exception as e:
            agent_run.delete()
            logger.error("Specialist '%s' dispatch failed: %s", agent_name, e)
            return f"Specialist '{agent_name}' dispatch failed: {e}"

    return dispatch_specialist


def make_check_reports(run):
    """Build a check_reports tool bound to the active run.

    The tool cannot inspect any other run — the run_id comes from the
    closure, not the model's arguments.
    """
    run_id = run.id

    @tool
    def check_reports(wait_seconds: int = 30) -> str:
        """Check which specialists still need attention for the current run.
        Blocks up to wait_seconds (max 120) for new completions via Redis Streams,
        then returns only agents that haven't been read yet (status='running' in DB).
        Also monitors the message board — returns early if a board message arrives
        so you can read it promptly.

        Already-read reports and dispatch failures are excluded to keep payloads small.
        Includes a progress summary line.

        Returns JSON: {progress: "14/26 done", agents: [{agent_run_id, agent_name, status}, ...]}

        Args:
            wait_seconds: How long to block waiting for new completions (default 30, max 120).
        """
        from django.conf import settings as django_settings
        from django.utils import timezone as tz

        from .containers import _get_redis_client, get_container_status
        from .models import AgentRun

        nonlocal_wait = max(1, min(wait_seconds, 120))
        agent_timeout = getattr(django_settings, 'FUZZYCLAW_AGENT_TIMEOUT', 600)
        hitl_timeout = getattr(django_settings, 'FUZZYCLAW_HITL_TIMEOUT', 1800)

        # Try Redis Streams first for instant notification.
        # Watch BOTH the completion stream AND the board stream so the
        # coordinator wakes up immediately when a board message arrives.
        r = _get_redis_client()
        stream_key = f"fuzzyclaw:run:{run_id}:done"
        board_key = f"fuzzyclaw:board:{run_id}"

        if r:
            try:
                deadline = time.time() + nonlocal_wait
                done_last_id = '$'
                board_last_id = '$'
                running_count = AgentRun.objects.filter(run_id=run_id, status='running').count()
                signals_received = 0
                board_wakeup = False
                while time.time() < deadline and signals_received < running_count and not board_wakeup:
                    remaining_ms = max(100, int((deadline - time.time()) * 1000))
                    streams = r.xread(
                        {stream_key: done_last_id, board_key: board_last_id},
                        block=remaining_ms,
                        count=100,
                    )
                    if streams:
                        coordinator_id = f"coordinator_{run_id}"
                        for s_key, entries in streams:
                            # Redis client may return bytes or str keys
                            key_str = s_key.decode() if isinstance(s_key, bytes) else s_key
                            if key_str == stream_key:
                                signals_received += len(entries)
                                done_last_id = entries[-1][0]
                            elif key_str == board_key:
                                board_last_id = entries[-1][0]
                                # Only wake up for messages addressed to coordinator or 'all'
                                for _, data in entries:
                                    to = data.get(b'to', data.get('to', ''))
                                    if isinstance(to, bytes):
                                        to = to.decode()
                                    if to in (coordinator_id, 'all', 'human'):
                                        board_wakeup = True
                                        break
            except Exception as e:
                logger.warning("Redis XREAD failed (falling back to filesystem): %s", e)

        # Progress summary — total excludes dispatch failures (deleted or pending)
        total = AgentRun.objects.filter(run_id=run_id, status__in=['running', 'completed', 'failed']).count()
        done = AgentRun.objects.filter(run_id=run_id, status__in=['completed', 'failed']).count()

        # Only query agents still needing attention (DB status='running')
        # Already-read reports (completed/failed) and dispatch failures (pending/deleted)
        # are excluded — the coordinator already has that data.
        agent_runs = AgentRun.objects.filter(run_id=run_id, status='running').order_by('id')
        comms_base = django_settings.BASE_DIR / 'comms'
        now = tz.now()

        statuses = []
        for ar in agent_runs:
            # Check if report file exists (filesystem polling)
            comms_dir = comms_base / str(ar.id)
            report_path = comms_dir / 'report.json'
            error_path = comms_dir / 'error.json'

            if report_path.is_file():
                statuses.append({
                    'agent_run_id': ar.id,
                    'agent_name': ar.agent_name,
                    'status': 'completed',
                })
            elif error_path.is_file():
                statuses.append({
                    'agent_run_id': ar.id,
                    'agent_name': ar.agent_name,
                    'status': 'failed',
                })
            else:
                # No report yet — check if container is still alive
                container_status = get_container_status(ar.id, ar.agent_name)

                if container_status == 'exited' or container_status == 'removed':
                    # Container gone without writing a report — crashed
                    _finalize_agent_run(ar, 'failed', f'Container {container_status} without writing a report.', now)
                    statuses.append({
                        'agent_run_id': ar.id,
                        'agent_name': ar.agent_name,
                        'status': 'crashed',
                    })
                elif ar.started_at and (now - ar.started_at).total_seconds() > _effective_timeout(ar, agent_timeout, hitl_timeout):
                    # Timed out — kill the container and finalize
                    effective = _effective_timeout(ar, agent_timeout, hitl_timeout)
                    _kill_timed_out_container(ar)
                    _finalize_agent_run(ar, 'failed', f'Agent timed out after {effective}s.', now)
                    statuses.append({
                        'agent_run_id': ar.id,
                        'agent_name': ar.agent_name,
                        'status': 'timed_out',
                    })
                else:
                    statuses.append({
                        'agent_run_id': ar.id,
                        'agent_name': ar.agent_name,
                        'status': 'running',
                    })

        result = {
            'progress': f'{done}/{total} agents done',
            'agents': statuses,
        }
        return json.dumps(result, indent=2)

    return check_reports


def _effective_timeout(agent_run, agent_timeout: int, hitl_timeout: int) -> int:
    """Return the effective timeout for an agent run.

    Agents with the 'message_board' tool may be waiting for human input,
    so they get the longer HITL timeout instead of the standard agent timeout.
    """
    try:
        agent_def = get_agent(agent_run.agent_name)
        if 'message_board' in agent_def.get('tools', []):
            return hitl_timeout
    except AgentNotFound:
        pass
    return agent_timeout


def _finalize_agent_run(agent_run, status: str, error_msg: str, now):
    """Mark an AgentRun as done and release its container slot."""
    from .containers import _release_container_slot

    agent_run.status = status
    agent_run.error_message = error_msg
    agent_run.completed_at = now
    agent_run.save(update_fields=['status', 'error_message', 'completed_at'])

    _release_container_slot(agent_run.id)

    logger.info("Finalized agent_run %d (%s): %s", agent_run.id, agent_run.agent_name, status)


def _kill_timed_out_container(agent_run):
    """Stop and remove a timed-out container."""
    from .containers import get_docker_client

    if not agent_run.container_id:
        return
    try:
        client = get_docker_client()
        container = client.containers.get(agent_run.container_id)
        container.stop(timeout=10)
        container.remove(force=True)
        logger.info("Killed timed-out container %s for agent_run %d", agent_run.container_id[:12], agent_run.id)
    except Exception as e:
        logger.warning("Failed to kill container for agent_run %d: %s", agent_run.id, e)


def make_read_report(run):
    """Build a read_report tool bound to the active run.

    The tool can only read reports for agent runs that belong to the bound
    run. A coordinator can't finalize or peek at unrelated work by passing
    a stale or hallucinated agent_run_id.
    """
    run_id = run.id

    @tool
    def read_report(agent_run_id: int) -> str:
        """Read a single specialist's report and finalize its AgentRun record.
        Call this after check_reports shows the agent has completed or failed.

        Args:
            agent_run_id: The AgentRun ID to read the report for. Must belong
                to the current run.
        """
        from .containers import _release_container_slot, read_agent_report
        from .models import AgentRun

        try:
            agent_run = AgentRun.objects.get(pk=agent_run_id, run_id=run_id)
        except AgentRun.DoesNotExist:
            return f"Error: AgentRun {agent_run_id} not found in the current run."

        # If already finalized, return the stored report
        if agent_run.status == 'completed' and agent_run.report:
            return agent_run.report
        if agent_run.status == 'failed' and agent_run.error_message:
            return f"Specialist '{agent_run.agent_name}' failed: {agent_run.error_message}"

        # Read from filesystem
        report_data, exit_code = read_agent_report(agent_run_id)

        # Release container slot — this agent is done regardless of outcome
        _release_container_slot(agent_run_id)

        if exit_code == 0:
            report_text = report_data.get('report', '')
            agent_run.status = 'completed'
            agent_run.report = report_text
            agent_run.raw_data = report_data
            agent_run.completed_at = timezone.now()
            agent_run.save(update_fields=['status', 'report', 'raw_data', 'completed_at'])
            logger.info("Read report for agent_run %d (%s): completed", agent_run_id, agent_run.agent_name)
            return report_text
        else:
            error_msg = report_data.get('error', f'Agent exited with code {exit_code}')
            agent_run.status = 'failed'
            agent_run.error_message = error_msg
            agent_run.raw_data = report_data
            agent_run.completed_at = timezone.now()
            agent_run.save(update_fields=['status', 'error_message', 'raw_data', 'completed_at'])
            logger.error(
                "Read report for agent_run %d (%s): failed — %s",
                agent_run_id, agent_run.agent_name, error_msg,
            )
            return f"Specialist '{agent_run.agent_name}' failed: {error_msg}"

    return read_report


def make_submit_coordinator_report(run):
    """Build a submit_coordinator_report tool bound to the active run.

    The tool cannot finalize any other run — the run_id comes from the
    closure, not the model's arguments.
    """
    @tool
    def submit_coordinator_report(report: str) -> str:
        """Submit the final coordinator synthesis report for the current run.

        Args:
            report: The final synthesis report text.
        """
        from .models import AgentRun

        active = AgentRun.objects.filter(
            run_id=run.id, status__in=('pending', 'running'),
        ).count()

        if active > 0:
            return (
                f"Cannot submit report: {active} agent(s) still active. "
                "Use check_reports to wait for them before finishing."
            )

        run.refresh_from_db()
        run.coordinator_report = report
        run.status = 'completed'
        run.completed_at = timezone.now()
        run.save(update_fields=['coordinator_report', 'status', 'completed_at'])
        return "Report submitted successfully."

    return submit_coordinator_report
