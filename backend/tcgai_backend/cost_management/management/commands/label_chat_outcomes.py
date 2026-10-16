"""
Label every conversation with its outcome category (chat_outcomes.py):
purchased / cart_no_purchase / purchase_intent / no_buying_intent, each
with its receipt. Ingest hooks keep labels fresh as new signals land;
this command is the backfill and the re-derivation path (e.g. after the
intent patterns change) -- it is idempotent, so re-running is safe.

Usage:
    python manage.py label_chat_outcomes
    python manage.py label_chat_outcomes --since 2026-06-01 --limit 500
    python manage.py label_chat_outcomes --dry-run
"""
from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand

from django.db import close_old_connections

from cost_management.chat_outcomes import label_chat
from cost_management.models import Chat


class Command(BaseCommand):
    help = "Label conversations with outcome categories (purchased / cart / intent / none)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            type=str,
            default="2026-06-01",
            help="Only label chats on/after this date (YYYY-MM-DD). Default: 2026-06-01.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Maximum chats to label (0 = all).",
        )
        parser.add_argument(
            "--llm",
            action="store_true",
            help="Use the AI intent classifier for the intent tier (needs ANTHROPIC_API_KEY); falls back to the rules per chat when unavailable.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would be labeled without writing anything.",
        )

    def handle(self, *args, **options):
        since = datetime.fromisoformat(options["since"])
        chats = Chat.objects.filter(timestamp__gte=since).order_by("timestamp")
        if options["limit"]:
            chats = chats[: options["limit"]]
        counts = Counter()
        total = 0
        errors = 0
        failed_ids = []
        for chat in chats.iterator():
            total += 1
            if options["dry_run"]:
                counts[chat.outcome or "unlabeled"] += 1
                continue
            # A long backfill must survive one bad chat and a stale DB
            # connection: refresh connections each pass and never let a
            # single failure abort the run (2026-10-16: the first --llm
            # backfill died ~240 chats in and took the whole pass with
            # it). Failures are counted and named, never hidden.
            close_old_connections()
            try:
                counts[label_chat(chat, use_llm=options["llm"])] += 1
            except Exception as exc:  # per-chat guard: keep going
                errors += 1
                failed_ids.append(chat.chat_id)
                self.stderr.write(f"label failed for {chat.chat_id}: {exc}")
        verb = "would label" if options["dry_run"] else "labeled"
        self.stdout.write(f"{verb} {total} chats since {options['since']}: {dict(counts)}")
        if errors:
            self.stdout.write(f"errors: {errors} chats failed: {failed_ids}")
