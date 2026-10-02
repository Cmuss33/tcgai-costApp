"""
Command to automatically audit unaudited conversations in batch.
Designed to be run via cron / scheduled jobs or manually from the CLI.

Usage:
    python manage.py auto_audit_chats
    python manage.py auto_audit_chats --limit 30
    python manage.py auto_audit_chats --dry-run
"""
from django.core.management.base import BaseCommand
from cost_management.models import Chat
from cost_management.month_utils import real_chats
from cost_management.views import score_single_chat


class Command(BaseCommand):
    help = "Automatically audits unaudited shopper conversations using Claude Haiku."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=25,
            help="Maximum number of conversations to audit (default: 25).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show conversations that would be audited without calling Claude.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        dry_run = options["dry_run"]

        candidate_chats = list(
            real_chats(Chat.objects.filter(evaluation_score__isnull=True))
            .order_by("-timestamp")[:limit]
        )

        if not candidate_chats:
            self.stdout.write(self.style.SUCCESS("No unaudited conversations found."))
            return

        self.stdout.write(f"Found {len(candidate_chats)} unaudited conversation(s) to audit.")

        if dry_run:
            for c in candidate_chats:
                self.stdout.write(f"  [DRY-RUN] Would audit chat: {c.chat_id}")
            self.stdout.write(self.style.WARNING("Dry run complete. No changes made."))
            return

        success_count = 0
        for c in candidate_chats:
            self.stdout.write(f"Auditing chat: {c.chat_id}...", ending="")
            score = score_single_chat(c)
            if score is not None:
                success_count += 1
                self.stdout.write(self.style.SUCCESS(f" Scored {score}%"))
            else:
                self.stdout.write(self.style.ERROR(" Failed"))

        est_cost = round(success_count * 0.00035, 4)
        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully audited {success_count}/{len(candidate_chats)} chats (~${est_cost:.4f} USD)."
            )
        )
