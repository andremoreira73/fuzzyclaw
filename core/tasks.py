"""Celery tasks for FuzzyClaw agent orchestration."""
import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=1)
def launch_coordinator(self, run_id: int):
    """Launch a coordinator agent for a given Run.

    Called from Django views (e.g. "Run Now" button) or from Celery Beat (scheduled runs).
    The coordinator reads the briefing, dispatches specialists, and writes the final report.
    """
    from .agent_runtime import run_coordinator
    from .models import Run

    try:
        run = Run.objects.select_related('briefing').get(pk=run_id)
    except Run.DoesNotExist:
        logger.error("Run %d not found", run_id)
        return

    if run.status not in ('pending',):
        logger.warning("Run %d has status '%s', expected 'pending'. Skipping.", run_id, run.status)
        return

    run.status = 'running'
    run.started_at = timezone.now()
    run.save(update_fields=['status', 'started_at'])

    logger.info("Starting coordinator for run %d (briefing: %s)", run_id, run.briefing.title)

    try:
        report = run_coordinator(run.briefing, run)

        run.refresh_from_db()
        run.status = 'completed'
        run.completed_at = timezone.now()
        if not run.coordinator_report:
            run.coordinator_report = report
        run.save(update_fields=['status', 'completed_at', 'coordinator_report'])

        logger.info("Run %d completed successfully", run_id)

    except Exception as e:
        logger.error("Run %d failed: %s", run_id, e, exc_info=True)
        run.status = 'failed'
        run.error_message = str(e)
        run.completed_at = timezone.now()
        run.save(update_fields=['status', 'error_message', 'completed_at'])
        raise

    finally:
        from .containers import cleanup_run
        try:
            cleanup_run(run_id)
        except Exception as e:
            logger.warning("Cleanup for run %d failed: %s", run_id, e)


@shared_task
def launch_briefing_scheduled(briefing_id: int):
    """Celery Beat entry point for scheduled briefings.

    Creates a Run with triggered_by='scheduled' and hands off to launch_coordinator.
    Skips if the briefing is inactive or already has a running run.
    """
    from .models import Briefing, Run

    with transaction.atomic():
        try:
            briefing = Briefing.objects.select_for_update().get(pk=briefing_id, is_active=True)
        except Briefing.DoesNotExist:
            logger.warning("Scheduled launch for briefing %d skipped (not found or inactive)", briefing_id)
            return

        if Run.objects.filter(briefing=briefing, status__in=('pending', 'running')).exists():
            logger.warning("Scheduled launch for briefing %d skipped (run already in progress)", briefing_id)
            return

        run = Run.objects.create(
            briefing=briefing,
            status='pending',
            triggered_by='scheduled',
        )

    logger.info("Scheduled run #%d created for briefing '%s'", run.id, briefing.title)
    launch_coordinator.delay(run.id)


@shared_task
def cleanup_exited_containers():
    """Remove exited fuzzyclaw-agent-* containers.

    Safe to run periodically — only removes stopped containers.
    """
    import docker

    try:
        client = docker.from_env()
    except Exception as e:
        logger.error("Failed to connect to Docker: %s", e)
        return

    prefix = settings.FUZZYCLAW_AGENT_IMAGE_PREFIX
    containers = client.containers.list(
        all=True,
        filters={'name': prefix, 'status': 'exited'},
    )

    removed = 0
    for container in containers:
        try:
            container.remove(force=True)
            removed += 1
        except Exception as e:
            logger.warning("Failed to remove container %s: %s", container.name, e)

    if removed:
        logger.info("Cleaned up %d exited agent container(s)", removed)
    return removed
