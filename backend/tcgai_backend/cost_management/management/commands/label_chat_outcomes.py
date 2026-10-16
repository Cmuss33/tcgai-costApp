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
        for chat in chats:
            total += 1
            if options["dry_run"]:
                counts[chat.outcome or "unlabeled"] += 1
                continue
            counts[label_chat(chat, use_llm=options["llm"])] += 1
        verb = "would label" if options["dry_run"] else "labeled"
        self.stdout.write(f"{verb} {total} chats since {options['since']}: {dict(counts)}")
