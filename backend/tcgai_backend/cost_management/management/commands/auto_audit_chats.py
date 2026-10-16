"""
Command to automatically audit unaudited conversations in batch.
Designed to be run via cron / scheduled jobs or manually from the CLI.

Usage:
    python manage.py auto_audit_chats
    python manage.py auto_audit_chats --limit 30
    python manage.py auto_audit_chats --dry-run
    python manage.py auto_audit_chats --rescore --since 2026-06-01 --limit 500
        (retro-grade already-scored chats onto the current rubric;
        superseded scores are preserved in evaluation_history)
"""
from django.core.management.base import BaseCommand
from cost_management.models import Chat
from cost_management.month_utils import real_chats
from cost_management.views import score_and_classify_chat, _conversation_completed


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
        parser.add_argument(
            "--rescore",
            action="store_true",
            help="Retro-grade: include chats that already have a score and "
                 "grade them again with the current evaluator (median "
                 "sampling + receipts). The superseded score is preserved "
                 "in each chat's evaluation_history, never destroyed.",
        )
        parser.add_argument(
            "--since",
            type=str,
            default=None,
            help="Only chats on/after this date (YYYY-MM-DD). Bounds the "
                 "blast radius of a --rescore pass.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        dry_run = options["dry_run"]
        rescore = options["rescore"]

        qs = Chat.objects.all()
        if not rescore:
            qs = qs.filter(evaluation_score__isnull=True)
        if options["since"]:
            from datetime import datetime
            from django.utils import timezone as dj_tz
            since = dj_tz.make_aware(datetime.strptime(options["since"], "%Y-%m-%d"))
            qs = qs.filter(timestamp__gte=since)

        candidate_chats = list(
            real_chats(qs)
            .order_by("-timestamp")[:limit]
        )
        # Only grade completed conversations (30+ min quiet) -- the list is
        # newest-first, so without this the cron grades chats mid-flight.
        candidate_chats = [c for c in candidate_chats if _conversation_completed(c)]

        if not candidate_chats:
            self.stdout.write(self.style.SUCCESS(
                "No conversations to rescore found." if rescore
                else "No unaudited conversations found."))
            return

        self.stdout.write(f"Found {len(candidate_chats)} unaudited conversation(s) to audit.")

        if dry_run:
            for c in candidate_chats:
                self.stdout.write(f"  [DRY-RUN] Would audit chat: {c.chat_id}")
            self.stdout.write(self.style.WARNING("Dry run complete. No changes made."))
            return

        success_count = 0
        for c in candidate_chats:
            previous = c.evaluation_score
            self.stdout.write(f"Auditing chat: {c.chat_id}...", ending="")
            score = score_and_classify_chat(c)
            if score is not None:
                success_count += 1
                note = f" (was {previous})" if rescore and previous is not None else ""
                self.stdout.write(self.style.SUCCESS(f" Scored {score}%{note}"))
            else:
                self.stdout.write(self.style.ERROR(" Failed"))

        est_cost = round(success_count * 0.0018, 4)
        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully audited {success_count}/{len(candidate_chats)} chats (~${est_cost:.4f} USD)."
            )
        )
