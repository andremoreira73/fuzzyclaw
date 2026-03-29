"""Dashboard views for FuzzyClaw web UI."""
import logging
import re
from datetime import datetime, timezone

import redis as redis_lib
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone as dj_timezone
from django.views.decorators.http import require_POST

from .models import AgentImage, AgentRun, Briefing, Run
from .scheduling import get_schedule_status, sync_schedule

logger = logging.getLogger(__name__)
from .registry import get_available_agents, get_available_skills


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@login_required
def dashboard(request):
    user_briefings = Briefing.objects.filter(owner=request.user)
    recent_runs = (
        Run.objects.filter(briefing__owner=request.user)
        .select_related('briefing')
        .order_by('-created_at')[:8]
    )
    active_count = user_briefings.filter(is_active=True).count()
    context = {
        'briefing_count': user_briefings.count(),
        'active_briefing_count': active_count,
        'briefing_sublabel': f"{active_count} active",
        'agent_count': len(get_available_agents()),
        'skill_count': len(get_available_skills()),
        'recent_runs': recent_runs,
        'total_agent_runs': AgentRun.objects.filter(run__briefing__owner=request.user).count(),
    }
    return render(request, 'core/dashboard.html', context)


# ---------------------------------------------------------------------------
# Briefings
# ---------------------------------------------------------------------------

@login_required
def briefing_list(request):
    briefings = Briefing.objects.filter(owner=request.user)
    context = {
        'nav': 'briefings',
        'briefings': briefings,
        'active_count': briefings.filter(is_active=True).count(),
        'breadcrumbs': [{'label': 'Briefings'}],
    }
    return render(request, 'core/briefing_list.html', context)


@login_required
def briefing_create(request):
    if request.method == 'POST':
        briefing = Briefing.objects.create(
            owner=request.user,
            title=request.POST.get('title', 'Untitled Briefing'),
            content=request.POST.get('content', ''),
            coordinator_model=request.POST.get('coordinator_model', 'gemini-2.5-pro'),
            schedule_text=request.POST.get('schedule_text', ''),
            is_active=request.POST.get('is_active') == 'on',
        )
        messages.success(request, f'Briefing "{briefing.title}" created.')

        if 'launch' in request.POST:
            run = Run.objects.create(
                briefing=briefing, status='pending', triggered_by='manual',
            )
            from .tasks import launch_coordinator
            launch_coordinator.delay(run.id)
            messages.info(request, f'Run #{run.id} launched.')
            return redirect('core:run_detail', pk=run.pk)

        return redirect('core:briefing_detail', pk=briefing.pk)

    context = {
        'nav': 'briefings',
        'model_choices': settings.FUZZYCLAW_MODEL_CHOICES,
        'breadcrumbs': [
            {'label': 'Briefings', 'url': '/briefings/'},
            {'label': 'New'},
        ],
    }
    return render(request, 'core/briefing_form.html', context)


@login_required
def briefing_detail(request, pk):
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)
    runs = briefing.runs.order_by('-created_at')[:20]
    context = {
        'nav': 'briefings',
        'briefing': briefing,
        'runs': runs,
        'model_choices': settings.FUZZYCLAW_MODEL_CHOICES,
        'toggle_url': f'/briefings/{briefing.pk}/toggle/',
        'model_url': f'/briefings/{briefing.pk}/model/',
        'schedule_status': get_schedule_status(briefing),
        'breadcrumbs': [
            {'label': 'Briefings', 'url': '/briefings/'},
            {'label': briefing.title},
        ],
    }
    return render(request, 'core/briefing_detail.html', context)


@login_required
@require_POST
def briefing_save(request, pk):
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)
    briefing.title = request.POST.get('title', briefing.title)
    briefing.content = request.POST.get('content', briefing.content)
    briefing.coordinator_model = request.POST.get('coordinator_model', briefing.coordinator_model)
    briefing.schedule_text = request.POST.get('schedule_text', briefing.schedule_text)
    briefing.save()
    messages.success(request, 'Briefing saved.')

    if 'launch' in request.POST:
        run = Run.objects.create(
            briefing=briefing, status='pending', triggered_by='manual',
        )
        from .tasks import launch_coordinator
        launch_coordinator.delay(run.id)
        messages.info(request, f'Run #{run.id} launched.')
        return redirect('core:run_detail', pk=run.pk)

    return redirect('core:briefing_detail', pk=briefing.pk)


@login_required
@require_POST
def briefing_toggle(request, pk):
    """HTMX endpoint: flip is_active and return the updated toggle partial."""
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)
    briefing.is_active = not briefing.is_active
    # Also save form fields if included from the page
    update_fields = ['is_active']
    schedule_text = request.POST.get('schedule_text')
    if schedule_text is not None:
        briefing.schedule_text = schedule_text.strip()
        update_fields.append('schedule_text')
    coordinator_model = request.POST.get('coordinator_model')
    if coordinator_model is not None:
        briefing.coordinator_model = coordinator_model
        update_fields.append('coordinator_model')
    briefing.save(update_fields=update_fields)
    # Sync schedule: pause/resume the PeriodicTask based on is_active
    try:
        sync_schedule(briefing)
    except Exception:
        logger.exception("Failed to sync schedule for briefing %d", briefing.pk)
    html = render_to_string('core/partials/toggle.html', {
        'name': 'is_active',
        'checked': briefing.is_active,
        'label': 'Active',
        'hint': "Inactive briefings won't run on schedule.",
        'toggle_url': f'/briefings/{briefing.pk}/toggle/',
    }, request=request)
    return HttpResponse(html)


@login_required
@require_POST
def briefing_model(request, pk):
    """HTMX endpoint: save coordinator_model and return the updated select partial."""
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)
    coordinator_model = request.POST.get('coordinator_model', briefing.coordinator_model)
    if coordinator_model != briefing.coordinator_model:
        briefing.coordinator_model = coordinator_model
        briefing.save(update_fields=['coordinator_model'])
    html = render_to_string('core/partials/model_select.html', {
        'selected': briefing.coordinator_model,
        'model_choices': settings.FUZZYCLAW_MODEL_CHOICES,
        'model_url': f'/briefings/{briefing.pk}/model/',
    }, request=request)
    return HttpResponse(html)


@login_required
@require_POST
def briefing_schedule(request, pk):
    """HTMX endpoint: save schedule_text, parse it, and sync the PeriodicTask."""
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)

    # Save form fields so the user doesn't have to Save first
    update_fields = []
    schedule_text = request.POST.get('schedule_text', '').strip()
    if schedule_text != (briefing.schedule_text or '').strip():
        briefing.schedule_text = schedule_text
        update_fields.append('schedule_text')
    coordinator_model = request.POST.get('coordinator_model')
    if coordinator_model and coordinator_model != briefing.coordinator_model:
        briefing.coordinator_model = coordinator_model
        update_fields.append('coordinator_model')
    if update_fields:
        briefing.save(update_fields=update_fields)

    try:
        result = sync_schedule(briefing)
    except Exception as e:
        logger.exception("Failed to parse schedule for briefing %d", briefing.pk)
        result = {'action': 'error', 'error': str(e)}

    schedule_status = get_schedule_status(briefing)
    html = render_to_string('core/partials/schedule_status.html', {
        'briefing': briefing,
        'schedule_status': schedule_status,
        'schedule_result': result,
    }, request=request)
    return HttpResponse(html)


@login_required
@require_POST
def briefing_launch(request, pk):
    briefing = get_object_or_404(Briefing, pk=pk, owner=request.user)
    run = Run.objects.create(
        briefing=briefing, status='pending', triggered_by='manual',
    )
    from .tasks import launch_coordinator
    launch_coordinator.delay(run.id)
    messages.info(request, f'Run #{run.id} launched for "{briefing.title}".')
    return redirect('core:run_detail', pk=run.pk)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@login_required
def run_detail(request, pk):
    run = get_object_or_404(
        Run.objects.select_related('briefing'),
        pk=pk,
        briefing__owner=request.user,
    )
    agent_runs = run.agent_runs.order_by('created_at')
    context = {
        'nav': 'briefings',
        'run': run,
        'agent_runs': agent_runs,
        'breadcrumbs': [
            {'label': 'Briefings', 'url': '/briefings/'},
            {'label': run.briefing.title, 'url': f'/briefings/{run.briefing.pk}/'},
            {'label': f'Run #{run.id}'},
        ],
    }
    return render(request, 'core/run_detail.html', context)


@login_required
def run_status(request, pk):
    """HTMX endpoint for polling run status."""
    run = get_object_or_404(
        Run.objects.select_related('briefing'),
        pk=pk,
        briefing__owner=request.user,
    )
    agent_runs = run.agent_runs.order_by('created_at')
    context = {'run': run, 'agent_runs': agent_runs}
    return render(request, 'core/partials/run_status.html', context)


# ---------------------------------------------------------------------------
# Agents & Skills (read-only)
# ---------------------------------------------------------------------------

@login_required
def agent_list(request):
    agents = get_available_agents()
    image_names = set(
        AgentImage.objects.filter(build_error='').values_list('agent_name', flat=True)
    )
    for agent in agents:
        agent['has_image'] = agent['name'] in image_names
    context = {
        'nav': 'agents',
        'agents': agents,
        'breadcrumbs': [{'label': 'Agents'}],
    }
    return render(request, 'core/agent_list.html', context)


@login_required
def skill_list(request):
    skills = get_available_skills()
    context = {
        'nav': 'skills',
        'skills': skills,
        'breadcrumbs': [{'label': 'Skills'}],
    }
    return render(request, 'core/skill_list.html', context)


# ---------------------------------------------------------------------------
# Message Board — all reads/writes go to Redis directly
# ---------------------------------------------------------------------------

_board_pool = redis_lib.ConnectionPool.from_url(
    settings.FUZZYCLAW_REDIS_URL, decode_responses=True,
)


def _get_board_redis():
    """Get a Redis client for board operations (shared connection pool)."""
    return redis_lib.Redis(connection_pool=_board_pool)


def _parse_ts(ts_str: str):
    """Parse an ISO timestamp string into a datetime, or None."""
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


@login_required
def board_messages(request, run_pk):
    """HTMX endpoint: return board messages as HTML partial.

    Reads directly from Redis Streams via XRANGE. No DB model involved.

    Query params:
      filter: 'all' (default) or 'human' (only messages to/from human)
    """
    run = get_object_or_404(
        Run.objects.select_related('briefing'),
        pk=run_pk,
        briefing__owner=request.user,
    )
    filter_mode = request.GET.get('filter', 'all')

    board_messages_list = []
    try:
        r = _get_board_redis()
        stream_key = f"fuzzyclaw:board:{run.id}"
        entries = list(reversed(r.xrevrange(stream_key, count=200)))

        for entry_id, data in entries:
            sender = data.get('from', '')
            recipient = data.get('to', '')

            if filter_mode == 'human':
                if recipient != 'human' and sender != 'human':
                    continue

            board_messages_list.append({
                'id': entry_id,
                'sender': sender,
                'recipient': recipient,
                'content': data.get('content', ''),
                'created_at': _parse_ts(data.get('ts', '')),
            })
    except Exception as e:
        logger.warning("board_messages: Redis read failed: %s", e)

    context = {
        'board_messages': board_messages_list,
        'run': run,
    }
    return render(request, 'core/partials/board_messages.html', context)


@login_required
@require_POST
def board_reply(request, run_pk):
    """HTMX endpoint: human posts a reply to the board.

    Writes directly to Redis Streams via XADD. No DB write.
    POST body: message (text with @recipient prefix)
    """
    run = get_object_or_404(
        Run.objects.select_related('briefing'),
        pk=run_pk,
        briefing__owner=request.user,
    )

    raw_message = request.POST.get('message', '').strip()
    if not raw_message:
        return HttpResponse(status=400)

    # Parse @recipient mentions and extract message body.
    # Multiple @mentions supported: "@agent_1 @agent_2 hello" sends to both.
    # No @mentions: defaults to coordinator for this run.
    mentions = re.findall(r'@(\S+)', raw_message)
    content = re.sub(r'@\S+\s*', '', raw_message).strip()

    if not content:
        return HttpResponse(status=400)

    recipients = mentions if mentions else [f"coordinator_{run.id}"]

    try:
        r = _get_board_redis()
        stream_key = f"fuzzyclaw:board:{run.id}"
        ts = dj_timezone.now().isoformat()
        for recipient in recipients:
            r.xadd(stream_key, {
                'from': 'human',
                'to': recipient,
                'content': content,
                'ts': ts,
            })
    except Exception as e:
        logger.warning("board_reply: Redis write failed: %s", e)
        return HttpResponse(
            '<div class="text-red-600 text-sm p-2">Failed to send message. Please try again.</div>',
            status=502,
        )

    # Return updated messages partial
    return board_messages(request, run_pk)


@login_required
def board_badge(request):
    """HTMX endpoint: return badge HTML showing count of runs with board messages."""
    waiting_count = 0
    try:
        r = _get_board_redis()
        recent_run_ids = list(
            Run.objects.filter(briefing__owner=request.user)
            .order_by('-created_at')
            .values_list('id', flat=True)[:20]
        )
        for run_id in recent_run_ids:
            stream_key = f"fuzzyclaw:board:{run_id}"
            if r.xlen(stream_key) > 0:
                waiting_count += 1
    except Exception as e:
        logger.warning("board_badge: Redis read failed: %s", e)

    context = {'waiting_count': waiting_count}
    return render(request, 'core/partials/board_badge.html', context)


@login_required
def board_participants(request, run_pk):
    """HTMX endpoint: return participant list for @autocomplete."""
    run = get_object_or_404(
        Run.objects.select_related('briefing'),
        pk=run_pk,
        briefing__owner=request.user,
    )

    enriched = []
    try:
        r = _get_board_redis()
        participants_key = f"fuzzyclaw:board:{run.id}:participants"
        members = r.smembers(participants_key)

        for p in sorted(members):
            parts = p.rsplit('_', 1)
            agent_name = parts[0] if parts else p
            agent_run_id = parts[1] if len(parts) == 2 else ''

            status = 'unknown'
            if agent_run_id.isdigit():
                ar = AgentRun.objects.filter(pk=int(agent_run_id), run=run).first()
                if ar:
                    status = ar.status

            enriched.append({
                'id': p,
                'agent_name': agent_name,
                'status': status,
            })
    except Exception as e:
        logger.warning("board_participants: Redis read failed: %s", e)

    context = {'participants': enriched, 'run': run}
    return render(request, 'core/partials/board_participants.html', context)


@login_required
def board_active_runs(request):
    """JSON endpoint: return list of runs with board messages (for run selector)."""
    data = []
    try:
        r = _get_board_redis()
        recent_runs = (
            Run.objects.filter(briefing__owner=request.user)
            .select_related('briefing')
            .order_by('-created_at')[:20]
        )
        for run in recent_runs:
            stream_key = f"fuzzyclaw:board:{run.id}"
            if r.xlen(stream_key) > 0:
                data.append({'id': run.id, 'title': run.briefing.title[:30]})
    except Exception as e:
        logger.warning("board_active_runs: Redis read failed: %s", e)

    return JsonResponse(data, safe=False)
