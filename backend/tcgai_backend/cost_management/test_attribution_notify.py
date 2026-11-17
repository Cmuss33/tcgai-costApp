"""Tests for the attributed-revenue Slack gate and the alert dashboard link.

Two defects from 2026-11-17, both visible in one Slack post:

1. The post announced "Attributed revenue: CAD 0.80" for order
   19032525602990, but commercial_impact excludes orders linked to probe
   (test) chats -- the linked chat was a test conversation, so the ledger
   showed CAD 0.00 while Slack celebrated. The announcement now follows
   the same rule as the rollup (stats_views._impact_orders).

2. Every alert's "Open the dashboard" link pointed at the API/ingest
   host (tcgai-costapp-24lh.onrender.com), whose root path 404s, because
   alerts.DASHBOARD_URL_DEFAULT named that host instead of the dashboard
   frontend (tcgai-costapp-1-0k1s.onrender.com).
"""
import json
import os
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from cost_management import alerts
from cost_management.models import AttributedOrder, Chat

SHOP = "test-shop.myshopify.com"


def _attribution_payload(chat_id, order_id):
    return {
        "chat_id": chat_id,
        "shop": SHOP,
        "order_id": order_id,
        "influenced_revenue": "0.80",
        "order_total": "1.80",
        "currency": "CAD",
        "order_created_at": "2026-11-17T14:39:00Z",
        "surfaces": ["chat"],
    }


class AttributedRevenueNotifyGateTests(TestCase):
    def _post(self, chat_id, order_id):
        with patch(
            "cost_management.views.refresh_outcome_for_chat_id"
        ), patch("cost_management.views.refresh_intent_v3_for_chat_id"), patch(
            "cost_management.views._notify_attributed_revenue"
        ) as notify:
            resp = self.client.post(
                reverse("log_attribution"),
                data=json.dumps(_attribution_payload(chat_id, order_id)),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        return notify

    def test_probe_linked_order_is_stored_but_not_announced(self):
        Chat.objects.create(
            chat_id="probe-chat-1", model="claude-haiku-4-5", is_probe=True
        )
        notify = self._post("probe-chat-1", "order-probe-1")
        notify.assert_not_called()
        # The row still lands -- only the announcement is gated.
        self.assertTrue(
            AttributedOrder.objects.filter(order_id="order-probe-1").exists()
        )

    def test_real_chat_order_is_announced(self):
        Chat.objects.create(
            chat_id="real-chat-1", model="claude-haiku-4-5", is_probe=False
        )
        notify = self._post("real-chat-1", "order-real-1")
        notify.assert_called_once()

    def test_unlinked_order_is_announced(self):
        # No Chat row: the rollup counts unlinked orders, so the post
        # stays consistent with the ledger.
        notify = self._post("no-such-chat", "order-unlinked-1")
        notify.assert_called_once()


class DashboardLinkTests(TestCase):
    def test_default_dashboard_link_is_the_frontend_host(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(alerts.DASHBOARD_URL_ENV, None)
            self.assertEqual(
                alerts._dashboard_link(""),
                "https://tcgai-costapp-1-0k1s.onrender.com",
            )

    def test_env_override_still_wins(self):
        with patch.dict(
            os.environ, {alerts.DASHBOARD_URL_ENV: "https://example.com/"}
        ):
            self.assertEqual(alerts._dashboard_link(""), "https://example.com")
