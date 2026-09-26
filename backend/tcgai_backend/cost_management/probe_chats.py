"""Identifies chats created by the AOP monitor's retired synthetic probe
(ENG-164).

From AOP #78 (2026-09-09 22:34 UTC) until the probe was removed, the AOP
monitor sent "Do you have any Pokemon booster boxes in stock?" to the live
chat endpoint about every six minutes, each time as a brand-new chat_id. The
chatbot's repeat-message breaker let five of them through per hour, which is
exactly flag_automated_chats' threshold (it flags buckets of MORE than five),
so none were flagged and every one showed up in the chat summary as a real
conversation.

A probe chat is recognized by its shape, not its text alone: it started
after the probe began, and EVERY message in it is the probe question
verbatim (the chatbot logs each LLM call of the one probe turn separately,
all with the same content). A shopper who asked that question and then kept
talking has other messages, so they are not matched.

Probe chats are flagged likely_automated, not deleted. That hides them from
the chat summary, conversation counts and insights (month_utils.real_chats)
while keeping their Message rows, so stats_views' cost-weighted bot share
still attributes the probe's real spend on the chat key to automated
traffic instead of inflating cost per real conversation.
"""
from datetime import datetime, timezone

from django.db.models import Count, F, Min, Q

PROBE_MESSAGE = "Do you have any Pokemon booster boxes in stock?"
# AOP #78 merged 22:34 UTC; its first probe arrived shortly after.
PROBE_STARTED_AT = datetime(2026, 9, 9, 22, 30, tzinfo=timezone.utc)


def unflagged_probe_chat_ids(Chat, Message):
    """chat_ids of not-yet-flagged chats that started after PROBE_STARTED_AT
    and whose every message is the probe question. Takes the models as
    arguments so a migration can pass its historical models
    (apps.get_model) instead of importing the live ones."""
    probe_ids = (
        Message.objects.values("chat_id")
        .annotate(
            total=Count("id"),
            probe=Count("id", filter=Q(content=PROBE_MESSAGE)),
            first_at=Min("timestamp"),
        )
        .filter(first_at__gte=PROBE_STARTED_AT, total=F("probe"))
        .values("chat_id")
    )
    return list(
        Chat.objects.filter(chat_id__in=probe_ids, likely_automated=False)
        .values_list("chat_id", flat=True)
    )
