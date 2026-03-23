"""Validate agent .md files on disk without writing to the database.

Usage:
    python manage.py check_agents
    python manage.py check_agents /custom/agents/dir/
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.registry import parse_agent_md, validate_agent


class Command(BaseCommand):
    help = 'Validate agent .md files on disk (no database writes).'

    def add_arguments(self, parser):
        parser.add_argument(
            'path', nargs='?', type=str, default=None,
            help='Directory containing agent .md files. Defaults to FUZZYCLAW_AGENTS_DIR.',
        )

    def handle(self, *args, **options):
        agents_dir = Path(options['path']) if options['path'] else settings.FUZZYCLAW_AGENTS_DIR

        if not agents_dir.is_dir():
            raise CommandError(f"'{agents_dir}' is not a directory.")

        md_files = sorted(agents_dir.glob('*.md'))
        if not md_files:
            self.stdout.write(self.style.WARNING(f"No .md files found in {agents_dir}"))
            return

        valid, errored = 0, 0

        for filepath in md_files:
            try:
                data = parse_agent_md(filepath)
            except ValueError as e:
                self.stdout.write(self.style.ERROR(f"  PARSE ERROR: {e}"))
                errored += 1
                continue

            errors = validate_agent(data)
            if errors:
                self.stdout.write(self.style.ERROR(f"  INVALID {filepath.name}:"))
                for err in errors:
                    self.stdout.write(self.style.ERROR(f"    - {err}"))
                errored += 1
            else:
                vols = data.get('volumes', [])
                vol_info = f", volumes={len(vols)}" if vols else ""
                self.stdout.write(self.style.SUCCESS(
                    f"  OK: {data['name']} (model={data['model_choice']}, "
                    f"tools={data['tools']}{vol_info})"
                ))
                valid += 1

        self.stdout.write('')
        self.stdout.write(f"Results: {valid} valid, {errored} errors")
        if errored:
            raise CommandError(f"{errored} agent(s) failed validation.")
