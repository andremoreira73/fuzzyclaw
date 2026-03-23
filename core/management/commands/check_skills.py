"""Validate skill directories on disk without writing to the database.

Usage:
    python manage.py check_skills
    python manage.py check_skills /custom/skills/dir/
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.registry import parse_skill_md, validate_skill


class Command(BaseCommand):
    help = 'Validate skill directories (containing SKILL.md) on disk (no database writes).'

    def add_arguments(self, parser):
        parser.add_argument(
            'path', nargs='?', type=str, default=None,
            help='Parent directory containing skill subdirectories. Defaults to FUZZYCLAW_SKILLS_DIR.',
        )

    def handle(self, *args, **options):
        skills_dir = Path(options['path']) if options['path'] else settings.FUZZYCLAW_SKILLS_DIR

        if not skills_dir.is_dir():
            raise CommandError(f"'{skills_dir}' is not a directory.")

        skill_files = sorted(skills_dir.glob('*/SKILL.md'))
        if not skill_files:
            self.stdout.write(self.style.WARNING(f"No SKILL.md files found in subdirectories of {skills_dir}"))
            return

        valid, errored = 0, 0

        for filepath in skill_files:
            try:
                data = parse_skill_md(filepath)
            except ValueError as e:
                self.stdout.write(self.style.ERROR(f"  PARSE ERROR: {e}"))
                errored += 1
                continue

            errors = validate_skill(data)
            if errors:
                self.stdout.write(self.style.ERROR(f"  INVALID {filepath.parent.name}:"))
                for err in errors:
                    self.stdout.write(self.style.ERROR(f"    - {err}"))
                errored += 1
            else:
                if not data['description']:
                    self.stdout.write(self.style.WARNING(f"  OK (no description): {data['name']}"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"  OK: {data['name']}"))
                valid += 1

        self.stdout.write('')
        self.stdout.write(f"Results: {valid} valid, {errored} errors")
        if errored:
            raise CommandError(f"{errored} skill(s) failed validation.")
