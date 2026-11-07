"""Tests for the relink_attribution_chat management command.

The command repairs one AttributedOrder row whose chat_id_raw was written
with the attribution session id instead of the conversation UUID (the
2026-10-13 PvpShoppe order 46259 case), leaving the row unlinked so
commercial_impact counts a test order's revenue. Fixtures mirror that
case's real shapes and values.
"""
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from cost_management.models import AttributedOrder, Chat

SHOP = "test-shop.myshopify.com"
ORDER_ID = "46259"
SESSION_ID_RAW = "attr_1791864956321_kuf3ku1hh"
CONVERSATION_ID = "1edf8bbc-38f6-4310-81fe-7dc751742401"
ORDER_WHEN = datetime(2026, 10, 13, 4, 16, tzinfo=dt_timezone.utc)


def _snapshot(order):
    return {
        f.name: getattr(order, f.attname)
        for f in AttributedOrder._meta.concrete_fields
    }


class RelinkAttributionChatTests(TestCase):
    def setUp(self):
        self.target_chat = Chat.objects.create(
            chat_id=CONVERSATION_ID, model="claude-haiku-4-5", is_probe=True
        )
        self.order = AttributedOrder.objects.create(
            chat=None,
            chat_id_raw=SESSION_ID_RAW,
            shop=SHOP,
            order_id=ORDER_ID,
            attribution_type="direct",
            influenced_revenue=Decimal("6.39"),
            order_total=Decimal("7.22"),
            currency="CAD",
            order_created_at=ORDER_WHEN,
            surfaces=["chat"],
            influence_score=0.45,
        )

    def _run(self, **overrides):
        kwargs = {
            "shop": SHOP,
            "order_id": ORDER_ID,
            "expect_chat_id_raw": SESSION_ID_RAW,
            "to_chat_id": CONVERSATION_ID,
        }
        kwargs.update(overrides)
        out = StringIO()
        call_command("relink_attribution_chat", stdout=out, **kwargs)
        return out.getvalue()

    def test_dry_run_reports_plan_and_writes_nothing(self):
        before = _snapshot(self.order)
        output = self._run()
        self.assertIn(SESSION_ID_RAW, output)
        self.assertIn(CONVERSATION_ID, output)
        self.assertIn("Dry run", output)
        self.order.refresh_from_db()
        self.assertEqual(_snapshot(self.order), before)
        self.assertIsNone(self.order.chat)

    def test_commit_relinks_and_changes_nothing_else(self):
        before = _snapshot(self.order)
        output = self._run(commit=True)
        self.assertIn("Relinked", output)
        self.order.refresh_from_db()
        after = _snapshot(self.order)
        self.assertEqual(after.pop("chat_id_raw"), CONVERSATION_ID)
        self.assertEqual(after.pop("chat"), CONVERSATION_ID)
        before.pop("chat_id_raw")
        before.pop("chat")
        self.assertEqual(after, before)
        self.assertEqual(self.order.chat.chat_id, CONVERSATION_ID)

    def test_wrong_expect_chat_id_raw_refuses_and_writes_nothing(self):
        before = _snapshot(self.order)
        with self.assertRaises(CommandError):
            self._run(expect_chat_id_raw="attr_someone_elses_session", commit=True)
        self.order.refresh_from_db()
        self.assertEqual(_snapshot(self.order), before)

    def test_missing_order_refuses(self):
        with self.assertRaises(CommandError):
            self._run(order_id="99999", commit=True)

    def test_missing_target_chat_refuses_and_writes_nothing(self):
        before = _snapshot(self.order)
        with self.assertRaises(CommandError):
            self._run(to_chat_id="no-such-conversation", commit=True)
        self.order.refresh_from_db()
        self.assertEqual(_snapshot(self.order), before)

    def test_multiple_matches_refuse(self):
        with patch(
            "cost_management.management.commands.relink_attribution_chat"
            ".AttributedOrder"
        ) as mock_model:
            mock_model.objects.filter.return_value = [self.order, self.order]
            with self.assertRaises(CommandError):
                self._run(commit=True)
        self.order.refresh_from_db()
        self.assertEqual(self.order.chat_id_raw, SESSION_ID_RAW)
        self.assertIsNone(self.order.chat)

    def test_already_relinked_rerun_is_clean_noop_in_both_modes(self):
        self._run(commit=True)
        # Re-run with the ORIGINAL (now stale) --expect value: the
        # already-relinked state is reported, not refused, in both modes.
        dry = self._run()
        self.assertIn("already relinked", dry)
        committed = self._run(commit=True)
        self.assertIn("already relinked", committed)
        self.order.refresh_from_db()
        self.assertEqual(self.order.chat_id_raw, CONVERSATION_ID)
        self.assertEqual(self.order.chat_id, CONVERSATION_ID)

    def test_relink_to_non_probe_chat_warns_but_still_relinks(self):
        plain_chat = Chat.objects.create(
            chat_id="a-real-conversation", model="claude-haiku-4-5", is_probe=False
        )
        output = self._run(to_chat_id=plain_chat.chat_id, commit=True)
        self.assertIn("WARNING", output)
        self.assertIn("is_probe: False", output)
        self.order.refresh_from_db()
        self.assertEqual(self.order.chat_id, plain_chat.chat_id)


class RelinkCommercialImpactIntegrationTests(TestCase):
    """The point of the repair: once the test order is linked to its
    probe-tagged conversation, commercial_impact stops counting it."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")
        spend = patch(
            "cost_management.stats_views._spend_for",
            return_value=(10.0, [], None),
        )
        spend.start()
        self.addCleanup(spend.stop)
        self.client.force_login(self.user)

        genuine_chat = Chat.objects.create(
            chat_id="genuine-conversation", model="claude-haiku-4-5", is_probe=False
        )
        AttributedOrder.objects.create(
            chat=genuine_chat,
            chat_id_raw="genuine-conversation",
            shop=SHOP,
            order_id="50001",
            influenced_revenue=Decimal("100.00"),
            order_total=Decimal("100.00"),
            currency="CAD",
            order_created_at=ORDER_WHEN,
        )
        # The drifted test order: unlinked, session id as chat_id_raw.
        Chat.objects.create(
            chat_id=CONVERSATION_ID, model="claude-haiku-4-5", is_probe=True
        )
        AttributedOrder.objects.create(
            chat=None,
            chat_id_raw=SESSION_ID_RAW,
            shop=SHOP,
            order_id=ORDER_ID,
            attribution_type="direct",
            influenced_revenue=Decimal("6.39"),
            order_total=Decimal("7.22"),
            currency="CAD",
            order_created_at=ORDER_WHEN,
            surfaces=["chat"],
            influence_score=0.45,
        )

    def tearDown(self):
        cache.clear()

    def _lifetime(self):
        return self.client.get(
            "/api/cost/commercial_impact/?month=lifetime"
        ).json()

    def test_revenue_counts_test_order_before_relink_and_excludes_it_after(self):
        before = self._lifetime()
        self.assertEqual(before["influenced_revenue"], 106.39)
        self.assertEqual(before["orders_count"], 2)
        self.assertEqual(before["unlinked_orders"], 1)

        out = StringIO()
        call_command(
            "relink_attribution_chat",
            shop=SHOP,
            order_id=ORDER_ID,
            expect_chat_id_raw=SESSION_ID_RAW,
            to_chat_id=CONVERSATION_ID,
            commit=True,
            stdout=out,
        )

        cache.clear()  # commercial_impact caches its lifetime payload
        after = self._lifetime()
        self.assertEqual(after["influenced_revenue"], 100.0)
        self.assertEqual(after["orders_count"], 1)
        self.assertEqual(after["unlinked_orders"], 0)
