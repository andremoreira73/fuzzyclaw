"""Register a run from a guest agent (Claude Code, Codex, etc.).

Creates a Briefing (if needed), a completed Run, and an AgentRun record
so the work appears on the FuzzyClaw dashboard.

Usage:
    # Report from a file:
    python manage.py register_run --briefing "Expenses Processing" \
        --agent claude-code --report-file /path/to/report.md

    # Report inline:
    python manage.py register_run --briefing "Expenses Processing" \
        --agent claude-code --report "Processed 5 invoices, total €2,340"

    # With structured data:
    python manage.py register_run --briefing "Expenses Processing" \
        --agent claude-code --report "..." \
        --raw-data '{"total_eur": 2340, "invoice_count": 5}'

    # With coordinator-level summary (separate from agent report):
    python manage.py register_run --briefing "Expenses Processing" \
        --agent claude-code --report "Detailed agent report..." \
        --coordinator-report "5 invoices processed successfully."
"""
import json
import sys

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import AgentRun, Briefing, Run


class Command(BaseCommand):
    help = 'Register a completed run from a guest agent.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--briefing', required=True,
            help='Briefing title. Created automatically if it does not exist.',
        )
        parser.add_argument(
            '--agent', required=True,
            help='Agent name (e.g. claude-code, codex, gemini-cli).',
        )
        parser.add_argument(
            '--report', default='',
            help='Agent report text (inline). Use --report-file for longer reports.',
        )
        parser.add_argument(
            '--report-file', default=None,
            help='Path to a file containing the agent report.',
        )
        parser.add_argument(
            '--coordinator-report', default='',
            help='Optional coordinator-level summary for the run.',
        )
        parser.add_argument(
            '--raw-data', default='{}',
            help='JSON string with structured data (e.g. \'{"total": 1234}\').',
        )
        parser.add_argument(
            '--user-notes', default='',
            help='Optional user notes to attach to the run.',
        )
        parser.add_argument(
            '--owner', default=None,
            help='Username of the briefing owner. Defaults to the first superuser.',
        )
        parser.add_argument(
            '--status', default='completed', choices=['completed', 'failed'],
            help='Run status. Default: completed.',
        )

    def handle(self, *args, **options):
        # --- Resolve owner ---
        owner = self._resolve_owner(options['owner'])

        # --- Resolve report text ---
        report = self._resolve_report(options['report'], options['report_file'])

        # --- Parse raw_data ---
        try:
            raw_data = json.loads(options['raw_data'])
        except json.JSONDecodeError as e:
            raise CommandError(f"Invalid --raw-data JSON: {e}")

        # --- Get or create briefing ---
        briefing, created = Briefing.objects.get_or_create(
            title=options['briefing'],
            owner=owner,
            defaults={
                'content': f"External workflow — runs logged by interactive coding agents.",
                'is_active': True,
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS(
                f"Created briefing: \"{briefing.title}\" (id={briefing.id})"
            ))
        else:
            self.stdout.write(f"Using existing briefing: \"{briefing.title}\" (id={briefing.id})")

        # --- Create run ---
        now = timezone.now()
        status = options['status']
        run = Run.objects.create(
            briefing=briefing,
            status=status,
            started_at=now,
            completed_at=now,
            coordinator_report=options['coordinator_report'],
            triggered_by='manual',
            user_notes=options['user_notes'],
        )

        # --- Create agent run ---
        agent_run = AgentRun.objects.create(
            run=run,
            agent_name=options['agent'],
            status=status,
            started_at=now,
            completed_at=now,
            report=report,
            raw_data=raw_data,
        )

        self.stdout.write(self.style.SUCCESS(
            f"Registered run #{run.id} → agent-run #{agent_run.id} "
            f"({options['agent']}, {status})"
        ))
        self.stdout.write(f"  Dashboard: /runs/{run.id}/")

    def _resolve_owner(self, username):
        if username:
            try:
                return User.objects.get(username=username)
            except User.DoesNotExist:
                raise CommandError(f"User '{username}' not found.")
        # Default: first superuser, then first user
        owner = User.objects.filter(is_superuser=True).first()
        if not owner:
            owner = User.objects.first()
        if not owner:
            raise CommandError("No users in the database. Create one first.")
        return owner

    def _resolve_report(self, inline, filepath):
        if filepath:
            try:
                with open(filepath) as f:
                    return f.read()
            except FileNotFoundError:
                raise CommandError(f"Report file not found: {filepath}")
            except OSError as e:
                raise CommandError(f"Cannot read report file: {e}")
        if inline:
            return inline
        # Check if stdin has data (piped input)
        if not sys.stdin.isatty():
            return sys.stdin.read()
        return ''
