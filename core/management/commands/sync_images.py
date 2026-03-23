"""Management command to build/rebuild Docker images for specialist agents."""
from django.core.management.base import BaseCommand, CommandError

from core.containers import sync_agent_images


class Command(BaseCommand):
    help = 'Scan agents/ directory and build/rebuild/remove Docker images as needed.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force-base',
            action='store_true',
            help='Force rebuild of the base image (e.g. after changing requirements-agent.txt).',
        )
        parser.add_argument(
            '--force-all',
            action='store_true',
            help='Force rebuild of ALL images (base + every agent).',
        )

    def handle(self, *args, **options):
        self.stdout.write("Syncing agent images...")

        result = sync_agent_images(
            force_base=options['force_base'] or options['force_all'],
            force_all=options['force_all'],
        )

        if result['built']:
            self.stdout.write(self.style.SUCCESS(
                f"  Built: {', '.join(result['built'])}"
            ))
        if result['removed']:
            self.stdout.write(self.style.WARNING(
                f"  Removed: {', '.join(result['removed'])}"
            ))
        if result['unchanged']:
            self.stdout.write(
                f"  Unchanged: {', '.join(result['unchanged'])}"
            )
        if result['errors']:
            for err in result['errors']:
                self.stdout.write(self.style.ERROR(
                    f"  ERROR ({err['agent']}): {err['error']}"
                ))

        total = len(result['built']) + len(result['unchanged'])
        self.stdout.write(f"\nTotal images: {total}")

        if result['errors']:
            raise CommandError(
                f"{len(result['errors'])} image(s) failed to build."
            )

        self.stdout.write(self.style.SUCCESS("Done."))
