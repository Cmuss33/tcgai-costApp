"""
Flags Chats whose opening message is part of an automated-traffic spike
(ENG-149/150 in shopify_chatbot_agent_2): a scripted caller pinging the live
chat endpoint with the identical message over and over, each time as a
brand-new chat_id, is not what a batch of real shoppers looks like. Letting
that traffic through corrupted both the dashboard's conversation count and
the AI-generated monthly insights (which sample the most-recent chats and can
be crowded out entirely by a high-frequency bot).

Detection: for each Chat's FIRST message (earliest timestamp), normalize the
text (trim, lowercase, collapse whitespace) and bucket by calendar hour. Any
bucket where the same normalized text opens more than THRESHOLD distinct
chats is treated as automated traffic, and every chat_id in it is flagged.

Idempotent and additive only: re-running never un-flags a chat, so a manual
override made in the admin always sticks. Safe to run repeatedly (e.g. daily)
to catch newly-arrived spikes; the main app's own live circuit breaker
(repeat-message-guard.server.js) already caps a single spike at 5/hour going
forward, so this is a backstop, not the only defense.

Usage:
    python manage.py flag_automated_chats [--dry-run] [--since-days N]
"""
import re
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone

from cost_management.models import Chat, Message

THRESHOLD = 5  # mirrors the main app's repeat-message-guard.server.js noiseThreshold


def normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class Command(BaseCommand):
    help = (
        "Flags Chat.likely_automated for chats whose opening message matches "
        "an hourly spike of identical text across many distinct chats."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", default=False,
            help="Report what would be flagged without writing anything.",
        )
        parser.add_argument(
            "--since-days", type=int, default=None,
            help="Only consider chats created in the last N days (default: all).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        since_days = options["since_days"]

        chats = Chat.objects.all()
        if since_days is not None:
            cutoff = timezone.now() - timezone.timedelta(days=since_days)
            chats = chats.filter(timestamp__gte=cutoff)

        chat_ids = list(chats.values_list("chat_id", flat=True))

        first_message_by_chat = {}
        for msg in (
            Message.objects.filter(chat_id__in=chat_ids)
            .order_by("chat_id", "timestamp", "id")
            .values("chat_id", "content", "timestamp")
        ):
            if msg["chat_id"] not in first_message_by_chat:
                first_message_by_chat[msg["chat_id"]] = msg

        buckets = defaultdict(list)
        for chat_id, msg in first_message_by_chat.items():
            text = normalize(msg["content"])
            if not text:
                continue
            hour = msg["timestamp"].replace(minute=0, second=0, microsecond=0)
            buckets[(text, hour)].append(chat_id)

        to_flag = set()
        for chat_ids_in_bucket in buckets.values():
            if len(chat_ids_in_bucket) > THRESHOLD:
                to_flag.update(chat_ids_in_bucket)

        already_flagged = set(
            Chat.objects.filter(chat_id__in=to_flag, likely_automated=True)
            .values_list("chat_id", flat=True)
        )
        newly_flagged = to_flag - already_flagged

        self.stdout.write(
            f"Automated-traffic pattern matched: {len(to_flag)} chats "
            f"({len(newly_flagged)} newly flagged, {len(already_flagged)} already flagged)"
        )

        if dry_run:
            self.stdout.write("Dry run — no changes made. Re-run without --dry-run to apply.")
            return

        if newly_flagged:
            Chat.objects.filter(chat_id__in=newly_flagged).update(likely_automated=True)
            self.stdout.write(self.style.SUCCESS(f"Flagged {len(newly_flagged)} chats as likely_automated."))
        else:
            self.stdout.write("Nothing new to flag.")
