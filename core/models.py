from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone


def validate_model_choice(value):
    """Validate that the model name exists in FUZZYCLAW_MODELS."""
    if value not in settings.FUZZYCLAW_MODELS:
        allowed = ', '.join(sorted(settings.FUZZYCLAW_MODELS.keys()))
        raise ValidationError(f'Unknown model "{value}". Allowed: {allowed}')


class Briefing(models.Model):
    """User-authored instructions for the coordinator agent. Lives in the DB."""
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='briefings')
    title = models.CharField(max_length=200)
    content = models.TextField(help_text='Markdown — steps, context, constraints for the coordinator.')
    coordinator_model = models.CharField(
        max_length=50,
        default='gemini-2.5-pro',
        validators=[validate_model_choice],
        help_text='Strong model for the coordinator.',
    )
    is_active = models.BooleanField(default=True)
    schedule_text = models.CharField(
        max_length=200,
        blank=True,
        help_text='Free-text schedule for the coordinator (e.g. "every weekday at 8am").',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['owner', 'is_active']),
        ]

    def __str__(self):
        return self.title


class Run(models.Model):
    """Execution record for a briefing. One briefing -> many runs."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    TRIGGER_CHOICES = [
        ('manual', 'Manual'),
        ('scheduled', 'Scheduled'),
    ]

    briefing = models.ForeignKey(Briefing, on_delete=models.CASCADE, related_name='runs')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    coordinator_report = models.TextField(
        blank=True,
        help_text='The coordinator final synthesis after all specialists report back.',
    )
    error_message = models.TextField(blank=True)
    triggered_by = models.CharField(max_length=20, choices=TRIGGER_CHOICES, default='manual')
    user_notes = models.TextField(
        blank=True,
        help_text='Free-form user annotations. Separate from the execution-managed fields above.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['briefing', 'status']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.briefing.title} — {self.created_at:%Y-%m-%d %H:%M} [{self.status}]"

    @classmethod
    def cleanup_old_runs(cls, weeks=6):
        """Delete runs older than the specified number of weeks."""
        cutoff = timezone.now() - timedelta(weeks=weeks)
        old_runs = cls.objects.filter(created_at__lt=cutoff)
        count = old_runs.count()
        old_runs.delete()
        return count


class AgentRun(models.Model):
    """A specialist agent's participation in a run."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name='agent_runs')
    agent_name = models.CharField(max_length=100, help_text='Agent name (matches filename in agents/ dir).')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    container_id = models.CharField(max_length=100, blank=True, help_text='Docker container ID.')
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    report = models.TextField(blank=True, help_text='What this specialist reported back.')
    raw_data = models.JSONField(default=dict, blank=True, help_text='Structured data from the specialist.')
    error_message = models.TextField(blank=True)
    user_notes = models.TextField(
        blank=True,
        help_text='Free-form user annotations. Separate from the execution-managed fields above.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['run', 'status']),
            models.Index(fields=['agent_name']),
        ]

    def __str__(self):
        return f"{self.agent_name} — {self.run.briefing.title} [{self.status}]"


class AgentImage(models.Model):
    """Tracks pre-built Docker images for specialist agents."""
    agent_name = models.CharField(max_length=100, unique=True)
    file_hash = models.CharField(max_length=64, help_text='SHA-256 hash of agent .md + skill deps.')
    image_tag = models.CharField(max_length=200, help_text='Docker image tag, e.g. fuzzyclaw-agent-summarizer:latest')
    built_at = models.DateTimeField(auto_now=True)
    build_error = models.TextField(blank=True)

    class Meta:
        ordering = ['agent_name']

    def __str__(self):
        return f"{self.agent_name} ({self.image_tag})"

    @property
    def has_error(self):
        return bool(self.build_error)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

@receiver(post_delete, sender=Briefing)
def cleanup_briefing_schedule(sender, instance, **kwargs):
    """Remove the PeriodicTask when a briefing is deleted."""
    from django_celery_beat.models import PeriodicTask
    PeriodicTask.objects.filter(name=f"briefing-{instance.id}").delete()
