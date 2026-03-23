"""Briefing schedule management — NL-to-cron parsing + PeriodicTask sync."""
import json
import logging

from django.conf import settings
from django_celery_beat.models import CrontabSchedule, PeriodicTask
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

TASK_NAME_PREFIX = 'briefing'


def _task_name(briefing_id: int) -> str:
    return f"{TASK_NAME_PREFIX}-{briefing_id}"


# ---------------------------------------------------------------------------
# NL → Cron (one cheap LLM call)
# ---------------------------------------------------------------------------

class CronSchedule(BaseModel):
    """Structured output for schedule parsing."""
    minute: str = Field(description="Cron minute field (0-59, *, */N)")
    hour: str = Field(description="Cron hour field (0-23, *, */N)")
    day_of_week: str = Field(description="Cron day of week (0=Sun, 1=Mon, ..., 6=Sat, *, 1-5 for weekdays)")
    day_of_month: str = Field(description="Cron day of month (1-31, *, */N)")
    month_of_year: str = Field(description="Cron month of year (1-12, *)")
    human_readable: str = Field(description="Short human-readable description, e.g. 'Weekdays at 9:00 AM'")


PARSE_SYSTEM_PROMPT = """You are a schedule parser. Convert the user's natural language schedule description into a cron expression.

The system timezone is {timezone}. When no timezone is specified, assume times are in {timezone}.
If the user specifies a different timezone (e.g. "9am EST", "14:00 UTC"), convert to {timezone} before outputting cron fields.

Return ONLY the structured output with these fields:
- minute, hour, day_of_week, day_of_month, month_of_year (standard cron fields, in {timezone})
- human_readable (short description like "Weekdays at 9:00 AM {timezone}")

Cron conventions:
- day_of_week: 0=Sunday, 1=Monday, ..., 6=Saturday. Use 1-5 for weekdays.
- Use * for "every" (e.g., every month = month_of_year: "*")
- Use */N for intervals (e.g., every 2 hours = hour: "*/2")

Examples (assuming {timezone}):
- "every weekday at 9am" → minute=0, hour=9, day_of_week=1-5, day_of_month=*, month_of_year=*
- "every Monday and Thursday at 3:30pm" → minute=30, hour=15, day_of_week=1,4, day_of_month=*, month_of_year=*
- "daily at midnight" → minute=0, hour=0, day_of_week=*, day_of_month=*, month_of_year=*
- "every 6 hours" → minute=0, hour=*/6, day_of_week=*, day_of_month=*, month_of_year=*
- "first of every month at 8am" → minute=0, hour=8, day_of_week=*, day_of_month=1, month_of_year=*
"""


def _get_celery_timezone() -> str:
    return getattr(settings, 'CELERY_TIMEZONE', 'UTC')


def parse_schedule_text(schedule_text: str) -> CronSchedule:
    """Parse natural language schedule into cron fields via a cheap LLM call."""
    from .agent_runtime import get_model

    model_name = getattr(settings, 'FUZZYCLAW_SCHEDULE_PARSER_MODEL', 'gemini-2.5-flash')
    model = get_model(model_name)
    structured_model = model.with_structured_output(CronSchedule)

    tz = _get_celery_timezone()
    system_prompt = PARSE_SYSTEM_PROMPT.format(timezone=tz)

    result = structured_model.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": schedule_text},
    ])

    logger.info(
        "Parsed schedule '%s' → %s %s %s %s %s (%s)",
        schedule_text, result.minute, result.hour, result.day_of_week,
        result.day_of_month, result.month_of_year, result.human_readable,
    )
    return result


# ---------------------------------------------------------------------------
# PeriodicTask sync
# ---------------------------------------------------------------------------

def sync_schedule(briefing) -> dict:
    """Create, update, pause, or remove the PeriodicTask for a briefing.

    Logic:
    - schedule_text blank → delete PeriodicTask
    - is_active False → pause (set enabled=False), preserve cron
    - is_active True + schedule_text → parse and create/update PeriodicTask
    - If schedule_text unchanged since last parse → re-enable without LLM call
    """
    task_name = _task_name(briefing.id)
    schedule_text = (briefing.schedule_text or '').strip()

    # Case 1: No schedule text → remove
    if not schedule_text:
        try:
            pt = PeriodicTask.objects.select_related('crontab').get(name=task_name)
            crontab = pt.crontab
            pt.delete()
            # Clean up orphaned crontab if no other task uses it
            if crontab and not PeriodicTask.objects.filter(crontab=crontab).exists():
                crontab.delete()
            logger.info("Removed schedule for briefing %d", briefing.id)
            return {'action': 'removed'}
        except PeriodicTask.DoesNotExist:
            return {'action': 'none'}

    # Case 2: Briefing inactive → pause
    if not briefing.is_active:
        try:
            pt = PeriodicTask.objects.get(name=task_name)
            if pt.enabled:
                pt.enabled = False
                pt.save(update_fields=['enabled'])
                logger.info("Paused schedule for briefing %d", briefing.id)
            return {'action': 'paused', 'human_readable': pt.description}
        except PeriodicTask.DoesNotExist:
            return {'action': 'none'}

    # Case 3: Active + has schedule_text → create/update
    # Check if we can skip re-parsing (schedule_text unchanged)
    try:
        existing = PeriodicTask.objects.get(name=task_name)
        try:
            has_valid_crontab = existing.crontab_id and existing.crontab is not None
        except CrontabSchedule.DoesNotExist:
            has_valid_crontab = False
        if has_valid_crontab and existing.description == schedule_text and existing.enabled:
            # Already up to date
            return {
                'action': 'unchanged',
                'human_readable': _human_readable_from_crontab(existing.crontab),
            }
        if has_valid_crontab and existing.description == schedule_text and not existing.enabled:
            # Same schedule, just re-enable
            existing.enabled = True
            existing.save(update_fields=['enabled'])
            return {
                'action': 'resumed',
                'human_readable': _human_readable_from_crontab(existing.crontab),
            }
        # Schedule text changed or crontab missing — fall through to re-parse
    except PeriodicTask.DoesNotExist:
        existing = None

    # Need to parse (new schedule or changed text)
    cron = parse_schedule_text(schedule_text)

    # Validate cron fields are not empty (LLM safety net)
    for field in ('minute', 'hour', 'day_of_week', 'day_of_month', 'month_of_year'):
        val = getattr(cron, field, '')
        if not val or not val.strip():
            raise ValueError(f"LLM returned empty cron field '{field}' for schedule: {schedule_text}")

    # Capture old crontab before updating so we can clean up if orphaned
    old_crontab = existing.crontab if existing else None

    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute=cron.minute,
        hour=cron.hour,
        day_of_week=cron.day_of_week,
        day_of_month=cron.day_of_month,
        month_of_year=cron.month_of_year,
    )

    defaults = {
        'crontab': crontab,
        'task': 'core.tasks.launch_briefing_scheduled',
        'args': json.dumps([briefing.id]),
        'enabled': True,
        'description': schedule_text,  # Store original text for change detection
    }

    pt, created = PeriodicTask.objects.update_or_create(
        name=task_name,
        defaults=defaults,
    )

    # Clean up old crontab if it changed and is now orphaned
    if old_crontab and old_crontab != crontab:
        if not PeriodicTask.objects.filter(crontab=old_crontab).exists():
            old_crontab.delete()

    action = 'created' if created else 'updated'
    logger.info(
        "%s schedule for briefing %d: %s",
        action.capitalize(), briefing.id, cron.human_readable,
    )

    return {
        'action': action,
        'human_readable': cron.human_readable,
        'cron': {
            'minute': cron.minute,
            'hour': cron.hour,
            'day_of_week': cron.day_of_week,
            'day_of_month': cron.day_of_month,
            'month_of_year': cron.month_of_year,
        },
    }


def _human_readable_from_crontab(crontab: CrontabSchedule) -> str:
    """Build a human-readable string from a CrontabSchedule object."""
    return f"{crontab.minute} {crontab.hour} {crontab.day_of_month} {crontab.month_of_year} {crontab.day_of_week}"


# ---------------------------------------------------------------------------
# Status query
# ---------------------------------------------------------------------------

def get_schedule_status(briefing) -> dict | None:
    """Return the current schedule status for a briefing, or None."""
    task_name = _task_name(briefing.id)
    try:
        pt = PeriodicTask.objects.select_related('crontab').get(name=task_name)
    except PeriodicTask.DoesNotExist:
        return None

    schedule_text = (briefing.schedule_text or '').strip()
    stale = pt.description != schedule_text

    crontab = pt.crontab
    cron_str = f"{crontab.minute} {crontab.hour} {crontab.day_of_month} {crontab.month_of_year} {crontab.day_of_week}"

    return {
        'enabled': pt.enabled,
        'human_readable': pt.description,
        'cron': cron_str,
        'stale': stale,
        'last_run_at': pt.last_run_at,
        'total_run_count': pt.total_run_count,
    }
