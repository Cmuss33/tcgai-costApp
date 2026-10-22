"""
Backfill Taxonomy v3 intent stamps over real chats (pack §6.3C).

Preserves every v2 stamp unchanged — this command writes ONLY the
``*_v3`` fields (see intent_v3.label_intent_v3). Population: real
PvpShoppe chats in the window, via month_utils.real_chats (probes,
automated traffic, shadowtest ids and non-chat surfaces excluded by
the app's existing real-chat rules).

Usage:
    python manage.py backfill_intent_v3
    python manage.py backfill_intent_v3 --since 2026-06-01 --until 2026-10-22 --llm
    python manage.py backfill_intent_v3 --dry-run
    python manage.py backfill_intent_v3 --review-output review-selection.json

VERIFY BY STAMPS, NEVER BY THIS COMMAND'S EXIT STATUS (standing
lesson from the v2 backfill, whose job readouts lied twice): after a
run, read intent_source_v3 / intent_classifier_version_v3 in the app
(the Chat Logs rows carry them, and chat_intents_v3_by_month counts
only chats carrying a v3 stamp) and confirm the counts before calling
the backfill done.
"""
import json
from collections import Counter
from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from cost_management.intent_v3 import build_review_selection, label_intent_v3
from cost_management.models import Chat
from cost_management.month_utils import apply_shop_filter, real_chats

DEFAULT_SHOP = "pvpshoppe.myshopify.com"


class Command(BaseCommand):
    help = "Backfill Taxonomy v3 intent stamps over real chats (v2 stamps preserved)."

    def add_arguments(self, parser):
        parser.add_argument("--since", type=str, default="2026-06-01",
                            help="Window start (YYYY-MM-DD, inclusive). Default: 2026-06-01.")
        parser.add_argument("--until", type=str, default="2026-10-22",
                            help="Window end (YYYY-MM-DD, inclusive). Default: 2026-10-22.")
        parser.add_argument("--shop", type=str, default=DEFAULT_SHOP,
                            help=f"Shop domain to backfill. Default: {DEFAULT_SHOP}. Pass '' for all shops.")
        parser.add_argument("--limit", type=int, default=0,
                            help="Maximum chats to label (0 = all).")
        parser.add_argument("--llm", action="store_true",
                            help="Use the AI v3 classifier (needs ANTHROPIC_API_KEY); falls back to rules per chat when unavailable.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Count the population without writing anything.")
        parser.add_argument("--review-output", type=str, default="",
                            help="Write the human-review selection (unknowns, low-confidence, stratified sample, AOP-linked) as JSON to this path.")

    def handle(self, *args, **options):
        since = timezone.make_aware(datetime.combine(datetime.fromisoformat(options["since"]).date(), time.min))
        until = timezone.make_aware(
            datetime.combine(datetime.fromisoformat(options["until"]).date(), time.min)
        ) + timedelta(days=1)
        shops = [options["shop"]] if options["shop"] else None
        chats = apply_shop_filter(
            real_chats(Chat.objects.filter(timestamp__gte=since, timestamp__lt=until)),
            shops,
        ).order_by("timestamp")
        total_population = chats.count()
        if options["limit"]:
            chats = chats[: options["limit"]]

        counts = Counter()
        labelled = []
        errors = 0
        failed_ids = []
        for chat in chats.iterator():
            if options["dry_run"]:
                counts[chat.intent_primary_v3 or "unstamped"] += 1
                continue
            # Same per-chat guard as label_chat_outcomes: one bad chat
            # (or a stale connection) must never abort the whole pass.
            close_old_connections()
            try:
                counts[label_intent_v3(chat, use_llm=options["llm"])] += 1
                labelled.append(chat)
            except Exception as exc:
                errors += 1
                failed_ids.append(chat.chat_id)
                self.stderr.write(f"v3 label failed for {chat.chat_id}: {exc}")

        verb = "would label" if options["dry_run"] else "labeled"
        self.stdout.write(
            f"{verb} {sum(counts.values())} of {total_population} real chats "
            f"{options['since']}..{options['until']}: {dict(counts)}"
        )
        # The receipt that matters: the stamps now on the rows.
        stamped = (
            apply_shop_filter(
                real_chats(Chat.objects.filter(timestamp__gte=since, timestamp__lt=until)),
                shops,
            )
            .exclude(intent_source_v3__isnull=True)
            .exclude(intent_source_v3="")
            .count()
        )
        self.stdout.write(
            f"verify by stamps: {stamped} chats now carry an intent_source_v3 stamp "
            f"(intent_classifier_version_v3=2026-10-22.1) — check these in the app, "
            f"not this job's status."
        )
        if options["review_output"] and not options["dry_run"]:
            selection = build_review_selection(labelled)
            with open(options["review_output"], "w", encoding="utf-8") as fh:
                json.dump(selection, fh, indent=2)
            self.stdout.write(
                f"review selection written to {options['review_output']}: "
                f"unknown={len(selection['unknown'])} "
                f"low_confidence={len(selection['low_confidence'])} "
                f"aop_linked={len(selection['aop_linked'])} "
                f"stratified_sample={len(selection['stratified_sample'])}"
            )
        if errors:
            self.stdout.write(f"errors: {errors} chats failed: {failed_ids}")
