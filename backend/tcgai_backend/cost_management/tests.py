import hashlib
import hmac
import json
import os
import time
from unittest.mock import patch, MagicMock

import requests

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import AttributedOrder, Chat, Message, AlertRule, AlertFiring, OperatorPreference
from .issue_trackers import GitHubIssueTracker, IssueRef, IssueTrackerError, LinearIssueTracker


PRODUCTS_SHOWN = {
    "primary": [
        {
            "id": "gid://shopify/Product/123",
            "title": "Charizard VMAX",
            "price": "89.99",
            "variant_id": "gid://shopify/ProductVariant/456",
            "image_url": "https://cdn.shopify.com/charizard.jpg",
            "url": "https://store.example.com/products/charizard-vmax",
            "vendor": "Pokemon",
            "product_type": "Single Card",
            "available": True,
        }
    ],
    "complementary": [
        {
            "id": "gid://shopify/Product/789",
            "title": "Ultra Pro Deck Box",
            "price": "12.99",
            "variant_id": "gid://shopify/ProductVariant/101",
            "image_url": "https://cdn.shopify.com/deckbox.jpg",
            "url": "https://store.example.com/products/deck-box",
            "vendor": "Ultra Pro",
            "product_type": "Accessory",
            "available": False,
        }
    ],
}


def make_log_message_payload(chat_id, products_shown=None):
    llm_formatted_message = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 2000,
        "system": "...",
        "messages": [{"role": "user"}, {"role": "assistant"}],
        "tools": [],
    }
    if products_shown is not None:
        llm_formatted_message["products_shown"] = products_shown

    return {
        "chat_id": chat_id,
        "content": "do you have any charizard cards",
        "llm_formatted_message": llm_formatted_message,
        "returned_content": "I found a Charizard VMAX for $89.99!",
        "llm_formatted_returned_message": {
            "role": "assistant",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
        "tokens_in": 100,
        "tokens_out": 50,
        "model": "claude-haiku-4-5-20251001",
    }


class LogMessageProductsShownTests(TestCase):
    def test_stores_products_shown_when_present(self):
        payload = make_log_message_payload("conv-with-products", PRODUCTS_SHOWN)

        response = self.client.post(
            "/api/cost/log_message/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        message = Message.objects.get(chat_id="conv-with-products")
        self.assertEqual(message.products_shown, PRODUCTS_SHOWN)

    def test_products_shown_is_null_when_absent(self):
        payload = make_log_message_payload("conv-without-products")

        response = self.client.post(
            "/api/cost/log_message/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        message = Message.objects.get(chat_id="conv-without-products")
        self.assertIsNone(message.products_shown)


class LogMessageAccumulationTests(TestCase):
    """The Chat row's tokens_in/tokens_out are an incrementally-cached
    running total (not derived from Message rows on read), updated inside
    select_for_update()+transaction.atomic() specifically so concurrent
    log_message calls for the same chat_id -- routine during a multi-step
    tool-use turn, which logs each LLM round-trip separately, sometimes
    within the same second -- can't race on the += and silently drop one
    side's update. This is a proportionate regression test for the
    accumulation logic itself (sequential calls must sum correctly); a true
    concurrent-write race is Django's/the DB's own well-established
    locking guarantee, not something worth a flaky threaded test here."""

    def test_two_calls_for_the_same_chat_sum_tokens_not_overwrite(self):
        first = make_log_message_payload("conv-accum")
        second = make_log_message_payload("conv-accum")
        second["tokens_in"] = 40
        second["tokens_out"] = 15

        for payload in (first, second):
            response = self.client.post(
                "/api/cost/log_message/",
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

        chat = Chat.objects.get(chat_id="conv-accum")
        self.assertEqual(chat.tokens_in, 100 + 40)
        self.assertEqual(chat.tokens_out, 50 + 15)
        self.assertEqual(Message.objects.filter(chat=chat).count(), 2)


class LogMessageCacheTokensTests(TestCase):
    """ENG-148: real, billed prompt-cache token counts, previously not sent
    by the chatbot at all -- see claude.server.js's sendCostLog fix."""

    def test_stores_cache_tokens_when_present(self):
        payload = make_log_message_payload("conv-with-cache")
        payload["cache_creation_tokens"] = 800
        payload["cache_read_tokens"] = 1200

        response = self.client.post(
            "/api/cost/log_message/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        message = Message.objects.get(chat_id="conv-with-cache")
        self.assertEqual(message.cache_creation_tokens, 800)
        self.assertEqual(message.cache_read_tokens, 1200)

    def test_defaults_to_zero_when_absent(self):
        """A sender that hasn't deployed the cache-reporting fix yet (or a
        turn with no caching activity) must keep working exactly as before."""
        payload = make_log_message_payload("conv-no-cache-fields")

        response = self.client.post(
            "/api/cost/log_message/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        message = Message.objects.get(chat_id="conv-no-cache-fields")
        self.assertEqual(message.cache_creation_tokens, 0)
        self.assertEqual(message.cache_read_tokens, 0)

    def test_null_cache_values_are_treated_as_zero_not_stored_as_null(self):
        payload = make_log_message_payload("conv-null-cache")
        payload["cache_creation_tokens"] = None
        payload["cache_read_tokens"] = None

        response = self.client.post(
            "/api/cost/log_message/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        message = Message.objects.get(chat_id="conv-null-cache")
        self.assertEqual(message.cache_creation_tokens, 0)
        self.assertEqual(message.cache_read_tokens, 0)


class GetChatIdsProductsShownCountTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(self.user)

    def _create_message(self, chat, products_shown):
        return Message.objects.create(
            chat=chat,
            content="hi",
            llm_formatted_message="{}",
            returned_content="hello",
            llm_formatted_returned_message="{}",
            tokens_in=10,
            tokens_out=5,
            model="claude-haiku-4-5",
            products_shown=products_shown,
        )

    def test_sums_products_across_a_chats_messages(self):
        chat = Chat.objects.create(chat_id="conv-with-products", model="claude-haiku-4-5")
        self._create_message(chat, PRODUCTS_SHOWN)  # 1 primary + 1 complementary
        self._create_message(chat, None)

        response = self.client.get("/api/cost/get_chat_ids/")

        data = response.json()
        result = next(c for c in data["results"] if c["chat_id"] == "conv-with-products")
        self.assertEqual(result["products_shown_count"], 2)

    def test_is_zero_when_no_products_shown(self):
        chat = Chat.objects.create(chat_id="conv-no-products", model="claude-haiku-4-5")
        self._create_message(chat, None)

        response = self.client.get("/api/cost/get_chat_ids/")

        data = response.json()
        result = next(c for c in data["results"] if c["chat_id"] == "conv-no-products")
        self.assertEqual(result["products_shown_count"], 0)


class GetChatIdsExcludesAutomatedChatsTests(TestCase):
    """ENG-149/150: the chat summary list (get_chat_ids) is the page a human
    reviews individual conversations on -- it must not still be dominated by
    the thousands of flagged bot chats just because monthly_stats/insights
    already exclude them. Not month-scoped like those, so without this the
    bot chats would be the overwhelming majority of every page."""

    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(self.user)

    def test_flagged_chats_are_excluded_from_results_and_total(self):
        Chat.objects.create(chat_id="real-1", model="claude-haiku-4-5")
        Chat.objects.create(chat_id="real-2", model="claude-haiku-4-5")
        for i in range(5):
            Chat.objects.create(chat_id=f"bot-{i}", model="claude-haiku-4-5", likely_automated=True)

        response = self.client.get("/api/cost/get_chat_ids/?limit=100")

        data = response.json()
        chat_ids = {c["chat_id"] for c in data["results"]}
        self.assertEqual(chat_ids, {"real-1", "real-2"})
        self.assertFalse(data["has_next"])

    def test_pagination_offsets_are_not_thrown_off_by_excluded_bot_chats(self):
        for i in range(3):
            Chat.objects.create(chat_id=f"real-{i}", model="claude-haiku-4-5")
        for i in range(50):
            Chat.objects.create(chat_id=f"bot-{i}", model="claude-haiku-4-5", likely_automated=True)

        response = self.client.get("/api/cost/get_chat_ids/?limit=2&offset=0")

        data = response.json()
        self.assertEqual(len(data["results"]), 2)
        self.assertTrue(all(not c["chat_id"].startswith("bot-") for c in data["results"]))
        self.assertTrue(data["has_next"])  # one real chat left, not the 50 bot chats
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["total_pages"], 2)


class GetChatIdsPaginationTests(TestCase):
    """Verifies that get_chat_ids returns total, page, total_pages,
    and supports page navigation across offsets and limits."""

    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(self.user)

    def test_pagination_fields_empty(self):
        response = self.client.get("/api/cost/get_chat_ids/")
        data = response.json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["total_pages"], 1)
        self.assertFalse(data["has_next"])
        self.assertEqual(data["results"], [])

    def test_multi_page_navigation(self):
        for i in range(25):
            Chat.objects.create(chat_id=f"chat-{i:02d}", model="claude-haiku-4-5")

        # Page 1: limit 10, offset 0 -> page 1 of 3
        res1 = self.client.get("/api/cost/get_chat_ids/?limit=10&offset=0")
        d1 = res1.json()
        self.assertEqual(d1["total"], 25)
        self.assertEqual(d1["page"], 1)
        self.assertEqual(d1["total_pages"], 3)
        self.assertTrue(d1["has_next"])
        self.assertEqual(len(d1["results"]), 10)

        # Page 2: limit 10, offset 10 -> page 2 of 3
        res2 = self.client.get("/api/cost/get_chat_ids/?limit=10&offset=10")
        d2 = res2.json()
        self.assertEqual(d2["total"], 25)
        self.assertEqual(d2["page"], 2)
        self.assertEqual(d2["total_pages"], 3)
        self.assertTrue(d2["has_next"])
        self.assertEqual(len(d2["results"]), 10)

        # Page 3: limit 10, offset 20 -> page 3 of 3
        res3 = self.client.get("/api/cost/get_chat_ids/?limit=10&offset=20")
        d3 = res3.json()
        self.assertEqual(d3["total"], 25)
        self.assertEqual(d3["page"], 3)
        self.assertEqual(d3["total_pages"], 3)
        self.assertFalse(d3["has_next"])
        self.assertEqual(len(d3["results"]), 5)

    def test_pagination_with_filters(self):
        # 3 audited chats, 2 unaudited
        for i in range(3):
            Chat.objects.create(chat_id=f"audited-{i}", model="claude-haiku-4-5", evaluation_score=95)
        for i in range(2):
            Chat.objects.create(chat_id=f"unaudited-{i}", model="claude-haiku-4-5", evaluation_score=None)

        res = self.client.get("/api/cost/get_chat_ids/?filter=unaudited&limit=10&offset=0")
        data = res.json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["total_pages"], 1)
        self.assertFalse(data["has_next"])
        self.assertEqual(len(data["results"]), 2)


class GetChatIdsCacheTokenTotalsTests(TestCase):
    """ENG-148: cache_creation_tokens/cache_read_tokens on each result are
    summed live from Message rows, not a cached Chat field -- deliberately
    not extending the same incrementally-cached-total pattern that needed a
    concurrency fix for tokens_in/tokens_out (see log_message)."""

    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(self.user)

    def _create_message(self, chat, cache_creation_tokens, cache_read_tokens):
        return Message.objects.create(
            chat=chat,
            content="hi",
            llm_formatted_message="{}",
            returned_content="hello",
            llm_formatted_returned_message="{}",
            tokens_in=10,
            tokens_out=5,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            model="claude-haiku-4-5",
        )

    def test_sums_cache_tokens_across_a_chats_messages(self):
        chat = Chat.objects.create(chat_id="conv-cached", model="claude-haiku-4-5")
        self._create_message(chat, 500, 1000)
        self._create_message(chat, 0, 2000)

        response = self.client.get("/api/cost/get_chat_ids/")

        data = response.json()
        result = next(c for c in data["results"] if c["chat_id"] == "conv-cached")
        self.assertEqual(result["cache_creation_tokens"], 500)
        self.assertEqual(result["cache_read_tokens"], 3000)

    def test_is_zero_when_no_cache_activity(self):
        chat = Chat.objects.create(chat_id="conv-no-cache", model="claude-haiku-4-5")
        self._create_message(chat, 0, 0)

        response = self.client.get("/api/cost/get_chat_ids/")

        data = response.json()
        result = next(c for c in data["results"] if c["chat_id"] == "conv-no-cache")
        self.assertEqual(result["cache_creation_tokens"], 0)
        self.assertEqual(result["cache_read_tokens"], 0)


class ChatInvestigationFieldsTests(TestCase):
    def test_new_chat_defaults_to_unflagged(self):
        chat = Chat.objects.create(chat_id="conv-defaults", model="claude-haiku-4-5")
        self.assertEqual(chat.investigation_status, "unflagged")
        self.assertEqual(chat.flag_reason, "")
        self.assertIsNone(chat.flagged_at)
        self.assertEqual(chat.flagged_by, "")
        self.assertIsNone(chat.github_issue_number)
        self.assertEqual(chat.github_issue_url, "")
        self.assertEqual(chat.linear_issue_id, "")
        self.assertEqual(chat.linear_issue_url, "")
        self.assertEqual(chat.flag_error, "")

    def test_status_choices_are_the_three_lifecycle_values(self):
        values = [value for value, _label in Chat.INVESTIGATION_STATUS_CHOICES]
        self.assertEqual(values, ["unflagged", "flagged", "resolved"])


class GetChatIdsInvestigationFieldsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(self.user)

    def test_get_chat_ids_row_includes_investigation_fields(self):
        Chat.objects.create(
            chat_id="conv-inv",
            model="claude-haiku-4-5",
            investigation_status="flagged",
            flag_reason="looks wrong",
            github_issue_url="https://github.com/x/y/issues/1",
            linear_issue_url="https://linear.app/x/issue/ABC-1",
        )

        response = self.client.get("/api/cost/get_chat_ids/")

        row = next(c for c in response.json()["results"] if c["chat_id"] == "conv-inv")
        self.assertEqual(row["investigation_status"], "flagged")
        self.assertEqual(row["flag_reason"], "looks wrong")
        self.assertEqual(row["github_issue_url"], "https://github.com/x/y/issues/1")
        self.assertEqual(row["linear_issue_url"], "https://linear.app/x/issue/ABC-1")

    def test_get_chat_ids_filters_and_preview(self):
        c1 = Chat.objects.create(chat_id="chat-good", model="claude-haiku-4-5", evaluation_score=95)
        c2 = Chat.objects.create(chat_id="chat-bad", model="claude-haiku-4-5", evaluation_score=60)
        c3 = Chat.objects.create(chat_id="chat-unscored", model="claude-haiku-4-5", evaluation_score=None)

        Message.objects.create(
            chat=c1, content="Do you have Charizard ex?", returned_content="Yes we do!",
            tokens_in=10, tokens_out=10, model="claude-haiku-4-5"
        )
        Message.objects.create(
            chat=c2, content="Need One Piece OP-05 box", returned_content="Sorry, out of stock",
            tokens_in=10, tokens_out=10, model="claude-haiku-4-5",
            products_shown={"primary": [{"title": "OP-05", "available": False}]}
        )

        # Test needs_attention filter (score < 75)
        res_attention = self.client.get("/api/cost/get_chat_ids/?filter=needs_attention")
        ids_attention = [c["chat_id"] for c in res_attention.json()["results"]]
        self.assertIn("chat-bad", ids_attention)
        self.assertNotIn("chat-good", ids_attention)

        # Test unaudited filter
        res_unaudited = self.client.get("/api/cost/get_chat_ids/?filter=unaudited")
        ids_unaudited = [c["chat_id"] for c in res_unaudited.json()["results"]]
        self.assertIn("chat-unscored", ids_unaudited)
        self.assertNotIn("chat-good", ids_unaudited)

        # Test search
        res_search = self.client.get("/api/cost/get_chat_ids/?search=Charizard")
        ids_search = [c["chat_id"] for c in res_search.json()["results"]]
        self.assertIn("chat-good", ids_search)
        self.assertNotIn("chat-bad", ids_search)

        # Test preview
        res_all = self.client.get("/api/cost/get_chat_ids/")
        all_json = res_all.json()
        good_row = next(c for c in all_json["results"] if c["chat_id"] == "chat-good")
        self.assertEqual(good_row["preview"], "Do you have Charizard ex?")

        # Test all-up KPIs (2 audited: 95 and 60 -> avg 77.5; 1 needs_attention: 60 < 75; total: 3)
        kpis = all_json["kpis"]
        self.assertEqual(kpis["audited_count"], 2)
        self.assertEqual(kpis["avg_score"], 77.5)
        self.assertEqual(kpis["needs_attention_count"], 1)
        self.assertEqual(kpis["total_conversations"], 3)

    def test_customer_inquiry_skips_generic_opening_greetings(self):
        import datetime
        from django.utils import timezone
        c = Chat.objects.create(chat_id="chat-greeting-test", model="claude-haiku-4-5")
        m1 = Message.objects.create(chat=c, content="Hello!", tokens_in=10, tokens_out=10, model="claude-haiku-4-5")
        m2 = Message.objects.create(chat=c, content="Do you have any Lorcana boosters in stock?", tokens_in=10, tokens_out=10, model="claude-haiku-4-5")
        # Ensure m1 is older than m2
        Message.objects.filter(id=m1.id).update(timestamp=timezone.now() - datetime.timedelta(minutes=5))

        res = self.client.get("/api/cost/get_chat_ids/")
        row = next(r for r in res.json()["results"] if r["chat_id"] == "chat-greeting-test")
        self.assertEqual(row["preview"], "Do you have any Lorcana boosters in stock?")

    def test_customer_inquiry_falls_back_to_greeting_if_only_message(self):
        c = Chat.objects.create(chat_id="chat-only-hi", model="claude-haiku-4-5")
        Message.objects.create(chat=c, content="hi there", tokens_in=10, tokens_out=10, model="claude-haiku-4-5")

        res = self.client.get("/api/cost/get_chat_ids/")
        row = next(r for r in res.json()["results"] if r["chat_id"] == "chat-only-hi")
        self.assertEqual(row["preview"], "hi there")

    def test_get_chat_ids_excludes_pre_june_2026_conversations(self):
        import datetime
        from django.utils import timezone
        # Legacy/pre-prod chat before June 1, 2026
        c_old = Chat.objects.create(chat_id="chat-may-2026", model="claude-haiku-4-5", evaluation_score=50)
        Chat.objects.filter(pk=c_old.pk).update(timestamp=timezone.make_aware(datetime.datetime(2026, 5, 31, 23, 59, 59)))

        # Chat on or after June 1, 2026
        c_new = Chat.objects.create(chat_id="chat-june-2026", model="claude-haiku-4-5", evaluation_score=90)
        Chat.objects.filter(pk=c_new.pk).update(timestamp=timezone.make_aware(datetime.datetime(2026, 6, 1, 0, 0, 0)))

        res = self.client.get("/api/cost/get_chat_ids/")
        data = res.json()

        result_ids = [c["chat_id"] for c in data["results"]]
        self.assertIn("chat-june-2026", result_ids)
        self.assertNotIn("chat-may-2026", result_ids)
        self.assertEqual(data["total"], 1)

        # All-up KPIs should only aggregate June 1, 2026 onwards (so avg_score is 90, not 70)
        kpis = data["kpis"]
        self.assertEqual(kpis["audited_count"], 1)
        self.assertEqual(kpis["avg_score"], 90.0)
        self.assertEqual(kpis["needs_attention_count"], 0)
        self.assertEqual(kpis["total_conversations"], 1)




def _fake_response(status_code, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


@override_settings(GITHUB_TOKEN="tok", GITHUB_ISSUE_REPO="acme/widgets")
class GitHubIssueTrackerTests(TestCase):
    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_posts_payload_and_parses_ref(self, mock_post):
        mock_post.return_value = _fake_response(
            201,
            {
                "number": 42,
                "node_id": "I_abc",
                "html_url": "https://github.com/acme/widgets/issues/42",
            },
        )

        ref = GitHubIssueTracker().create_issue("A title", "A body")

        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        self.assertEqual(url, "https://api.github.com/repos/acme/widgets/issues")
        self.assertEqual(kwargs["json"], {"title": "A title", "body": "A body"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(ref, IssueRef(id="I_abc", number=42,
                                       url="https://github.com/acme/widgets/issues/42"))

    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_raises_on_non_2xx(self, mock_post):
        mock_post.return_value = _fake_response(422, text="Validation failed")

        with self.assertRaises(IssueTrackerError) as ctx:
            GitHubIssueTracker().create_issue("t", "b")

        self.assertEqual(ctx.exception.tracker, "github")
        self.assertEqual(ctx.exception.operation, "create_issue")
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("Validation failed", ctx.exception.detail)

    @patch("cost_management.issue_trackers.requests.post")
    def test_add_label_posts_to_labels_endpoint(self, mock_post):
        mock_post.return_value = _fake_response(200, [])
        ref = IssueRef(id="I_abc", number=42, url="https://github.com/acme/widgets/issues/42")

        GitHubIssueTracker().add_label(ref, "agent:queued")

        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        self.assertEqual(url, "https://api.github.com/repos/acme/widgets/issues/42/labels")
        self.assertEqual(kwargs["json"], {"labels": ["agent:queued"]})

    @patch("cost_management.issue_trackers.requests.post")
    def test_add_comment_posts_body(self, mock_post):
        mock_post.return_value = _fake_response(201, {"id": 1})
        ref = IssueRef(id="I_abc", number=42, url="https://github.com/acme/widgets/issues/42")

        GitHubIssueTracker().add_comment(ref, "Linked Linear issue: https://linear.app/x/ABC-1")

        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        self.assertEqual(url, "https://api.github.com/repos/acme/widgets/issues/42/comments")
        self.assertEqual(kwargs["json"], {"body": "Linked Linear issue: https://linear.app/x/ABC-1"})

    @patch("cost_management.issue_trackers.requests.post")
    def test_add_label_raises_on_error(self, mock_post):
        mock_post.return_value = _fake_response(404, text="Not Found")
        ref = IssueRef(id="I_abc", number=42, url="u")

        with self.assertRaises(IssueTrackerError):
            GitHubIssueTracker().add_label(ref, "agent:queued")

    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_wraps_request_exception(self, mock_post):
        mock_post.side_effect = requests.Timeout("connection timed out")

        with self.assertRaises(IssueTrackerError) as ctx:
            GitHubIssueTracker().create_issue("t", "b")

        self.assertEqual(ctx.exception.tracker, "github")
        self.assertEqual(ctx.exception.operation, "create_issue")

    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_wraps_invalid_json(self, mock_post):
        resp = _fake_response(200, {})
        resp.json.side_effect = ValueError("no json")
        resp.text = "<html>gateway error</html>"
        mock_post.return_value = resp

        with self.assertRaises(IssueTrackerError) as ctx:
            GitHubIssueTracker().create_issue("t", "b")

        self.assertEqual(ctx.exception.tracker, "github")
        self.assertIn("invalid JSON", ctx.exception.detail)


@override_settings(LINEAR_API_KEY="lin_key", LINEAR_TEAM_ID="team-123", LINEAR_PROJECT_ID="proj-456")
class LinearIssueTrackerTests(TestCase):
    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_sends_mutation_and_parses_ref(self, mock_post):
        mock_post.return_value = _fake_response(
            200,
            {
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "uuid-1",
                            "identifier": "SHO-7",
                            "url": "https://linear.app/professor-meta/issue/SHO-7",
                        },
                    }
                }
            },
        )

        ref = LinearIssueTracker().create_issue("A title", "A body")

        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        self.assertEqual(url, "https://api.linear.app/graphql")
        self.assertEqual(kwargs["headers"]["Authorization"], "lin_key")
        self.assertEqual(kwargs["timeout"], 10)
        variables = kwargs["json"]["variables"]["input"]
        self.assertEqual(variables["teamId"], "team-123")
        self.assertEqual(variables["projectId"], "proj-456")
        self.assertEqual(variables["title"], "A title")
        self.assertEqual(variables["description"], "A body")
        self.assertIn("issueCreate", kwargs["json"]["query"])
        self.assertEqual(ref.id, "uuid-1")
        self.assertIsNone(ref.number)
        self.assertEqual(ref.url, "https://linear.app/professor-meta/issue/SHO-7")

    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_raises_on_graphql_errors(self, mock_post):
        mock_post.return_value = _fake_response(
            200, {"errors": [{"message": "project not found"}]}
        )

        with self.assertRaises(IssueTrackerError) as ctx:
            LinearIssueTracker().create_issue("t", "b")

        self.assertEqual(ctx.exception.tracker, "linear")
        self.assertIn("project not found", ctx.exception.detail)

    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_raises_on_http_error(self, mock_post):
        mock_post.return_value = _fake_response(401, text="Unauthorized")

        with self.assertRaises(IssueTrackerError) as ctx:
            LinearIssueTracker().create_issue("t", "b")

        self.assertEqual(ctx.exception.status, 401)

    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_wraps_request_exception(self, mock_post):
        mock_post.side_effect = requests.Timeout("connection timed out")

        with self.assertRaises(IssueTrackerError) as ctx:
            LinearIssueTracker().create_issue("t", "b")

        self.assertEqual(ctx.exception.tracker, "linear")
        self.assertEqual(ctx.exception.operation, "create_issue")

    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_wraps_invalid_json(self, mock_post):
        resp = _fake_response(200, {})
        resp.json.side_effect = ValueError("no json")
        resp.text = "<html>gateway error</html>"
        mock_post.return_value = resp

        with self.assertRaises(IssueTrackerError) as ctx:
            LinearIssueTracker().create_issue("t", "b")

        self.assertEqual(ctx.exception.tracker, "linear")
        self.assertIn("invalid JSON", ctx.exception.detail)

    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_raises_on_missing_issue_in_response(self, mock_post):
        mock_post.return_value = _fake_response(
            200, {"data": {"issueCreate": {"success": False, "issue": None}}}
        )

        with self.assertRaises(IssueTrackerError) as ctx:
            LinearIssueTracker().create_issue("t", "b")

        self.assertEqual(ctx.exception.tracker, "linear")
        self.assertIn("unexpected response", ctx.exception.detail)


INVESTIGATION_ENV = dict(
    GITHUB_TOKEN="tok",
    GITHUB_ISSUE_REPO="acme/widgets",
    GITHUB_TRIGGER_LABEL="agent:queued",
    LINEAR_API_KEY="lin_key",
    LINEAR_TEAM_ID="team-123",
    LINEAR_PROJECT_ID="proj-456",
    COST_APP_PUBLIC_URL="https://costapp.example.com",
)


@override_settings(**INVESTIGATION_ENV)
class FlagChatHappyPathTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reviewer", password="pw")
        self.client.force_login(self.user)
        self.chat = Chat.objects.create(chat_id="conv-flag-1", model="claude-haiku-4-5",
                                        intent="return_request", tokens_in=100, tokens_out=50)
        Message.objects.create(
            chat=self.chat, content="i want to return my order",
            llm_formatted_message="{}", returned_content="Sure, I can help with that.",
            llm_formatted_returned_message="{}", tokens_in=100, tokens_out=50,
            model="claude-haiku-4-5",
        )

    def _post(self, body=None):
        return self.client.post(
            "/api/cost/flag_chat/",
            data=json.dumps({"chat_id": "conv-flag-1", "reason": "Bot gave a wrong refund policy"}
                            if body is None else body),
            content_type="application/json",
        )

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_flag_creates_both_issues_and_persists(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef
        mock_gh.create_issue.return_value = IssueRef(
            id="I_1", number=7, url="https://github.com/acme/widgets/issues/7")
        mock_linear.create_issue.return_value = IssueRef(
            id="lin-uuid", number=None, url="https://linear.app/pm/issue/SHO-9")

        response = self._post()

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["investigation_status"], "flagged")
        self.assertEqual(data["github_issue_url"], "https://github.com/acme/widgets/issues/7")
        self.assertEqual(data["linear_issue_url"], "https://linear.app/pm/issue/SHO-9")
        self.assertEqual(data["flag_error"], "")

        chat = Chat.objects.get(chat_id="conv-flag-1")
        self.assertEqual(chat.investigation_status, "flagged")
        self.assertEqual(chat.flag_reason, "Bot gave a wrong refund policy")
        self.assertEqual(chat.flagged_by, "reviewer")
        self.assertIsNotNone(chat.flagged_at)
        self.assertEqual(chat.github_issue_number, 7)
        self.assertEqual(chat.github_issue_url, "https://github.com/acme/widgets/issues/7")
        self.assertEqual(chat.linear_issue_id, "lin-uuid")
        self.assertEqual(chat.linear_issue_url, "https://linear.app/pm/issue/SHO-9")
        self.assertEqual(chat.flag_error, "")

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_flag_issue_body_has_reason_metadata_and_transcript(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef
        mock_gh.create_issue.return_value = IssueRef(id="I_1", number=7, url="https://gh/7")
        mock_linear.create_issue.return_value = IssueRef(id="lin", number=None, url="https://lin/9")

        self._post()

        gh_title, gh_body = mock_gh.create_issue.call_args[0][0], mock_gh.create_issue.call_args[0][1]
        self.assertIn("conv-flag-1", gh_title)
        self.assertIn("Bot gave a wrong refund policy", gh_body)
        self.assertIn("## Flag reason", gh_body)
        self.assertIn("## Chat metadata", gh_body)
        self.assertIn("return_request", gh_body)
        self.assertIn("https://costapp.example.com/chats?chat=conv-flag-1", gh_body)
        self.assertIn("## Transcript", gh_body)
        self.assertIn("i want to return my order", gh_body)
        self.assertIn("Sure, I can help with that.", gh_body)

        # GitHub gets the trigger label; Linear description carries the GH url;
        # GitHub gets a back-link comment.
        mock_gh.add_label.assert_called_once()
        self.assertEqual(mock_gh.add_label.call_args[0][1], "agent:queued")
        linear_body = mock_linear.create_issue.call_args[0][1]
        self.assertIn("GitHub issue: https://gh/7", linear_body)
        mock_gh.add_comment.assert_called_once()
        self.assertIn("https://lin/9", mock_gh.add_comment.call_args[0][1])

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_issue_body_uses_no_user_text_fallback_for_empty_content(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef
        mock_gh.create_issue.return_value = IssueRef(id="I_1", number=7, url="https://gh/7")
        mock_linear.create_issue.return_value = IssueRef(id="lin", number=None, url="https://lin/9")
        Message.objects.filter(chat=self.chat).update(content="")

        self._post()

        gh_body = mock_gh.create_issue.call_args[0][1]
        self.assertIn("*(no user text recorded)*", gh_body)

    @override_settings(COST_APP_PUBLIC_URL="https://costapp.example.com/")
    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_deep_link_has_no_double_slash_when_public_url_has_trailing_slash(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef
        mock_gh.create_issue.return_value = IssueRef(id="I_1", number=7, url="https://gh/7")
        mock_linear.create_issue.return_value = IssueRef(id="lin", number=None, url="https://lin/9")

        self._post()

        gh_body = mock_gh.create_issue.call_args[0][1]
        self.assertIn("https://costapp.example.com/chats?chat=conv-flag-1", gh_body)
        self.assertNotIn("com//chats", gh_body)

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_issue_body_is_truncated_when_transcript_is_huge(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef
        mock_gh.create_issue.return_value = IssueRef(id="I_1", number=7, url="https://gh/7")
        mock_linear.create_issue.return_value = IssueRef(id="lin", number=None, url="https://lin/9")
        Message.objects.filter(chat=self.chat).update(returned_content="x" * 70000)

        self._post()

        gh_body = mock_gh.create_issue.call_args[0][1]
        self.assertLessEqual(len(gh_body), 65536)
        self.assertTrue(gh_body.endswith(
            "*(transcript truncated — see the Cost app link above for the full conversation)*"
        ))


@override_settings(**INVESTIGATION_ENV)
class FlagChatValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reviewer", password="pw")
        self.client.force_login(self.user)
        self.chat = Chat.objects.create(chat_id="conv-v", model="claude-haiku-4-5")

    def _post(self, body):
        return self.client.post("/api/cost/flag_chat/", data=json.dumps(body),
                                content_type="application/json")

    @patch("cost_management.investigation_views.github_tracker")
    def test_blank_reason_is_400_and_calls_no_tracker(self, mock_gh):
        resp = self._post({"chat_id": "conv-v", "reason": "   "})
        self.assertEqual(resp.status_code, 400)
        mock_gh.create_issue.assert_not_called()

    @patch("cost_management.investigation_views.github_tracker")
    def test_unknown_chat_is_404(self, mock_gh):
        resp = self._post({"chat_id": "nope", "reason": "something"})
        self.assertEqual(resp.status_code, 404)
        mock_gh.create_issue.assert_not_called()

    @patch("cost_management.investigation_views.github_tracker")
    def test_already_flagged_with_linear_id_is_409(self, mock_gh):
        Chat.objects.filter(pk="conv-v").update(
            investigation_status="flagged", github_issue_number=3,
            github_issue_url="https://gh/3", linear_issue_id="lin-x",
            linear_issue_url="https://lin/x")
        resp = self._post({"chat_id": "conv-v", "reason": "again"})
        self.assertEqual(resp.status_code, 409)
        mock_gh.create_issue.assert_not_called()

    @override_settings(LINEAR_TEAM_ID="")
    @patch("cost_management.investigation_views.github_tracker")
    def test_missing_setting_is_503_with_missing_list(self, mock_gh):
        resp = self._post({"chat_id": "conv-v", "reason": "x"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("LINEAR_TEAM_ID", resp.json()["missing"])
        mock_gh.create_issue.assert_not_called()

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_non_json_content_type_is_415_and_calls_no_tracker(self, mock_gh, mock_linear):
        resp = self.client.post(
            "/api/cost/flag_chat/",
            data=json.dumps({"chat_id": "conv-v", "reason": "something"}),
            content_type="text/plain",
        )
        self.assertEqual(resp.status_code, 415)
        mock_gh.create_issue.assert_not_called()
        mock_linear.create_issue.assert_not_called()


@override_settings(**INVESTIGATION_ENV)
class FlagChatPartialFailureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reviewer", password="pw")
        self.client.force_login(self.user)
        self.chat = Chat.objects.create(chat_id="conv-p", model="claude-haiku-4-5")
        Message.objects.create(chat=self.chat, content="hi", llm_formatted_message="{}",
                               returned_content="hello", llm_formatted_returned_message="{}",
                               tokens_in=1, tokens_out=1, model="claude-haiku-4-5")

    def _post(self):
        return self.client.post(
            "/api/cost/flag_chat/",
            data=json.dumps({"chat_id": "conv-p", "reason": "bad answer"}),
            content_type="application/json",
        )

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_github_create_failure_is_502_and_persists_nothing(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueTrackerError
        mock_gh.create_issue.side_effect = IssueTrackerError("github", "create_issue", 500, "boom")

        resp = self._post()

        self.assertEqual(resp.status_code, 502)
        mock_linear.create_issue.assert_not_called()
        chat = Chat.objects.get(pk="conv-p")
        self.assertEqual(chat.investigation_status, "unflagged")
        self.assertIsNone(chat.github_issue_number)

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_label_failure_is_soft_and_persists_flag(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef, IssueTrackerError
        mock_gh.create_issue.return_value = IssueRef(id="I", number=5, url="https://gh/5")
        mock_gh.add_label.side_effect = IssueTrackerError("github", "add_label", 422, "no label")
        mock_linear.create_issue.return_value = IssueRef(id="lin", number=None, url="https://lin/5")

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        chat = Chat.objects.get(pk="conv-p")
        self.assertEqual(chat.investigation_status, "flagged")
        self.assertIn("trigger label", chat.flag_error)

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_linear_failure_persists_github_and_returns_linear_error(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef, IssueTrackerError
        mock_gh.create_issue.return_value = IssueRef(id="I", number=6, url="https://gh/6")
        mock_linear.create_issue.side_effect = IssueTrackerError("linear", "create_issue", 400, "bad project")

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        self.assertIn("linear_error", resp.json())
        self.assertIn("linear create failed", resp.json()["flag_error"])
        chat = Chat.objects.get(pk="conv-p")
        self.assertEqual(chat.investigation_status, "flagged")
        self.assertEqual(chat.github_issue_number, 6)
        self.assertEqual(chat.linear_issue_id, "")
        self.assertIn("linear create failed", chat.flag_error)

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_comment_failure_is_soft(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef, IssueTrackerError
        mock_gh.create_issue.return_value = IssueRef(id="I", number=8, url="https://gh/8")
        mock_gh.add_comment.side_effect = IssueTrackerError("github", "add_comment", 500, "oops")
        mock_linear.create_issue.return_value = IssueRef(id="lin", number=None, url="https://lin/8")

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        chat = Chat.objects.get(pk="conv-p")
        self.assertEqual(chat.investigation_status, "flagged")
        self.assertEqual(chat.linear_issue_id, "lin")
        self.assertIn("back-link comment", chat.flag_error)

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_linear_retry_branch_only_calls_linear(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef
        Chat.objects.filter(pk="conv-p").update(
            investigation_status="flagged", flag_reason="bad answer",
            github_issue_number=9, github_issue_url="https://gh/9", linear_issue_id="")
        mock_linear.create_issue.return_value = IssueRef(id="lin-retry", number=None, url="https://lin/9")

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        mock_gh.create_issue.assert_not_called()
        mock_linear.create_issue.assert_called_once()
        chat = Chat.objects.get(pk="conv-p")
        self.assertEqual(chat.linear_issue_id, "lin-retry")
        self.assertEqual(chat.linear_issue_url, "https://lin/9")
        self.assertEqual(chat.flag_error, "")

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_sequential_double_submit_creates_one_github_issue(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef
        mock_gh.create_issue.return_value = IssueRef(id="I", number=10, url="https://gh/10")
        mock_linear.create_issue.return_value = IssueRef(id="lin", number=None, url="https://lin/10")

        first = self._post()
        second = self._post()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(mock_gh.create_issue.call_count, 1)


from datetime import timedelta

from django.core.cache import cache
from django.utils.timezone import now as _now


CANNED_INSIGHTS = {
    "headline": "One Piece singles are the top request.",
    "top_requests": [
        {"topic": "One Piece single cards", "count": 4, "share_pct": 40,
         "examples": ["conv-1", "conv-2"]},
    ],
    "unmet_needs": [
        {"gap": "Grading / PSA submission questions", "gap_type": "capability", "count": 2,
         "summary": "Bot has no grading info and defers to email.", "examples": ["conv-3"]},
    ],
    "product_demand": [
        {"product": "Charizard VMAX", "count": 3, "status": "out_of_stock", "examples": ["conv-4"]},
    ],
    "recommendations": [
        {"title": "Tune catalog search", "detail": "…", "impact": "low", "effort": "medium effort",
         "addresses": "catalog gap", "evidence_count": 3, "examples": ["conv-9"]},
        {"title": "Load store policies", "detail": "…", "impact": "high", "effort": "low effort",
         "addresses": "policy gap", "evidence_count": 9, "examples": ["conv-3"]},
        {"title": "Connect order-status lookup", "detail": "…", "impact": "high", "effort": "medium effort",
         "addresses": "capability gap", "evidence_count": 7, "examples": ["conv-5"]},
    ],
}


class ReportInsightsSchemaTests(TestCase):
    """See the "Deliberately declared LAST" comment on REPORT_INSIGHTS_TOOL:
    observed live (2026-09-17) that with "headline" as the tool schema's
    FIRST property, the model wrote a complete, specific headline while
    every evidence list (top_requests/unmet_needs/product_demand/
    recommendations) came back empty. Field order in a tool call is a much
    more mechanical lever than prose instructions, so headline must stay
    last and the evidence lists must carry a minItems floor."""

    def test_headline_is_the_last_declared_property(self):
        from .insights_views import REPORT_INSIGHTS_TOOL

        keys = list(REPORT_INSIGHTS_TOOL["input_schema"]["properties"].keys())
        self.assertEqual(keys[-1], "headline")

    def test_evidence_lists_have_a_minitems_floor(self):
        from .insights_views import REPORT_INSIGHTS_TOOL

        props = REPORT_INSIGHTS_TOOL["input_schema"]["properties"]
        self.assertEqual(props["top_requests"]["minItems"], 1)
        self.assertEqual(props["unmet_needs"]["minItems"], 1)
        self.assertEqual(props["product_demand"]["minItems"], 1)
        self.assertEqual(props["recommendations"]["minItems"], 3)
        self.assertEqual(props["recommendations"]["maxItems"], 6)

    def test_evidence_lists_have_a_maxitems_cap(self):
        """Bounding worst-case output size reduces how often a rich report
        exceeds INSIGHTS_MAX_TOKENS and gets cut off mid-generation (see
        InsightsTruncationTests)."""
        from .insights_views import (
            MAX_DEMAND_ITEMS, MAX_TOP_REQUESTS, MAX_UNMET_NEEDS, REPORT_INSIGHTS_TOOL,
        )

        props = REPORT_INSIGHTS_TOOL["input_schema"]["properties"]
        self.assertEqual(props["top_requests"]["maxItems"], MAX_TOP_REQUESTS)
        self.assertEqual(props["unmet_needs"]["maxItems"], MAX_UNMET_NEEDS)
        self.assertEqual(props["product_demand"]["maxItems"], MAX_DEMAND_ITEMS)


class InsightsTruncationTests(TestCase):
    """Observed live (2026-09-17): a tool call truncated at max_tokens
    produced a structurally-plausible but garbled result (see
    InsightsSummaryTests.test_malformed_model_output_is_sanitized_not_passed_through
    for the exact shape) rather than an obvious error. stop_reason ==
    "max_tokens" is the one reliable signal from the API itself that a
    result can't be trusted -- _generate_insights must treat it as a
    failure, not silently return the partial block.input."""

    def _mock_client(self, stop_reason, content=None):
        message = MagicMock(stop_reason=stop_reason, content=content or [])
        client = MagicMock()
        client.messages.create.return_value = message
        return client

    def test_raises_when_truncated_at_max_tokens(self):
        from .insights_views import _generate_insights

        client = self._mock_client("max_tokens")
        with patch("anthropic.Anthropic", return_value=client):
            with self.assertRaises(ValueError):
                _generate_insights(["<conversation id=\"c-1\">hi</conversation>"], "2026-09", 61)

    def test_returns_the_tool_input_on_a_normal_completion(self):
        from .insights_views import _generate_insights

        block = MagicMock(type="tool_use", input={"headline": "ok"})
        block.name = "report_insights"  # MagicMock(name=...) sets the mock's own repr name, not this attr
        client = self._mock_client("tool_use", content=[block])
        with patch("anthropic.Anthropic", return_value=client):
            result = _generate_insights(["<conversation id=\"c-1\">hi</conversation>"], "2026-09", 61)

        self.assertEqual(result, {"headline": "ok"})

    def test_requests_the_larger_token_budget(self):
        from .insights_views import INSIGHTS_MAX_TOKENS, _generate_insights

        block = MagicMock(type="tool_use", input={})
        block.name = "report_insights"
        client = self._mock_client("tool_use", content=[block])
        with patch("anthropic.Anthropic", return_value=client):
            _generate_insights(["<conversation id=\"c-1\">hi</conversation>"], "2026-09", 61)

        self.assertEqual(client.messages.create.call_args.kwargs["max_tokens"], INSIGHTS_MAX_TOKENS)
        self.assertGreater(INSIGHTS_MAX_TOKENS, 4096)


class InsightsPromptGroundingTests(TestCase):
    """_build_prompt is pure string-building split out of _generate_insights
    precisely so this grounding instruction can be checked without a real
    API call -- see _generate_insights' docstring."""

    def test_prompt_states_the_exact_total_and_forbids_recounting_it(self):
        from .insights_views import _build_prompt

        prompt = _build_prompt(["<conversation id=\"c-1\">hi</conversation>"], "2026-09", 61)

        self.assertIn("exactly 61 conversations", prompt)
        self.assertIn("never recount or estimate it", prompt)
        self.assertIn("must be exactly 61", prompt)

    def test_prompt_requires_headline_claims_to_be_backed_by_a_structured_item(self):
        """A headline naming a specific gap/product with no matching
        unmet_needs/product_demand entry reads as a finding with no evidence
        behind it -- the store owner relies on that evidence list (with
        example conversation ids) to trust the finding."""
        from .insights_views import _build_prompt

        prompt = _build_prompt(["<conversation id=\"c-1\">hi</conversation>"], "2026-09", 61)

        self.assertIn("isn't already one of the items above", prompt)

    def test_prompt_orders_evidence_lists_before_the_headline(self):
        """Observed live (2026-09-17): with headline declared/instructed first,
        the model wrote a full, specific headline while top_requests,
        unmet_needs, product_demand, and recommendations all came back `[]` --
        a headline with no evidence behind it at all. The prompt must instruct
        the lists to be filled in before the headline, matching the tool
        schema's field order (see REPORT_INSIGHTS_TOOL)."""
        from .insights_views import _build_prompt

        prompt = _build_prompt(["<conversation id=\"c-1\">hi</conversation>"], "2026-09", 61)

        lists_pos = prompt.index("- top_requests:")
        headline_pos = prompt.index("- headline:")
        self.assertLess(lists_pos, headline_pos)
        self.assertIn("this list may never be empty", prompt)

    def test_prompt_asks_for_forward_looking_unmet_needs_wording(self):
        from .insights_views import _build_prompt

        prompt = _build_prompt(["<conversation id=\"c-1\">hi</conversation>"], "2026-09", 61)

        self.assertIn("forward-looking opportunity", prompt)


class InsightsSummaryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")
        # cost_commentary is orthogonal to what these tests exercise (the
        # transcript-based customer-insights narrative) -- stub it so these
        # tests stay hermetic instead of hitting the real Anthropic/cost
        # adapter calls _finalize now also triggers. See
        # InsightsSummaryCostCommentaryTests for dedicated coverage.
        p = patch("cost_management.insights_views.cost_commentary_for", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        cache.clear()

    def _make_conversations(self, count, when=None, with_customer_text=0, prefix="conv"):
        when = when or _now()
        for i in range(count):
            chat = Chat.objects.create(chat_id=f"{prefix}-{i}", model="claude-haiku-4-5")
            Chat.objects.filter(pk=chat.pk).update(timestamp=when)
            Message.objects.create(
                chat=chat,
                content="do you have charizard" if i < with_customer_text else "",
                llm_formatted_message="{}",
                returned_content="Yes, we have a Charizard VMAX for $89.99.",
                llm_formatted_returned_message="{}",
                tokens_in=10, tokens_out=5, model="claude-haiku-4-5",
            )

    def test_requires_login(self):
        response = self.client.get("/api/cost/insights_summary/")
        self.assertEqual(response.status_code, 401)

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    def test_generates_and_stores_current_month_snapshot(self, mock_gen):
        self._make_conversations(6, with_customer_text=2)
        self.client.force_login(self.user)

        response = self.client.get("/api/cost/insights_summary/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["top_requests"], CANNED_INSIGHTS["top_requests"])
        self.assertEqual(data["unmet_needs"], CANNED_INSIGHTS["unmet_needs"])
        self.assertEqual(data["product_demand"], CANNED_INSIGHTS["product_demand"])
        self.assertEqual(data["conversations_analyzed"], 6)
        self.assertEqual(data["conversations_with_customer_text"], 2)
        self.assertFalse(data["cached"])
        self.assertIn("generated_at", data)
        self.assertTrue(any(m["is_current"] for m in data["available_months"]))
        mock_gen.assert_called_once()

        from cost_management.models import InsightsSnapshot
        first_of_month = _now().date().replace(day=1)
        snap = InsightsSnapshot.objects.get(month=first_of_month)
        self.assertEqual(snap.conversations_analyzed, 6)

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    def test_passes_the_dashboard_conversation_count_as_the_headline_ground_truth(self, mock_gen):
        """The model must not be left to recount/estimate the month's total
        conversation volume itself for the headline -- that's how it can land
        on a different number (e.g. "roughly 45") than the dashboard's own
        KPI (e.g. 61) for the same month. _build_payload must hand it the
        authoritative count (len(chats), same value as conversations_analyzed)
        to anchor that claim to."""
        self._make_conversations(6, with_customer_text=2)
        self.client.force_login(self.user)

        self.client.get("/api/cost/insights_summary/")

        total_conversations = mock_gen.call_args.args[2]
        self.assertEqual(total_conversations, 6)

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    def test_likely_automated_chats_are_excluded_from_the_sample(self, mock_gen):
        """ENG-149/150: a high-frequency bot pings the live chat endpoint with
        the same message on a schedule -- at 200-most-recent-chats-per-month
        sampling, that traffic alone could crowd out every real conversation
        from the analysis. Chats flagged likely_automated must never reach
        the transcript sample or the analyzed count."""
        self._make_conversations(6, with_customer_text=2, prefix="real")
        for i in range(20):
            chat = Chat.objects.create(chat_id=f"bot-{i}", model="claude-haiku-4-5", likely_automated=True)
            Chat.objects.filter(pk=chat.pk).update(timestamp=_now())
            Message.objects.create(
                chat=chat, content="Do you have any Pokemon booster boxes in stock?",
                llm_formatted_message="{}", returned_content="Yes! We have several in stock.",
                llm_formatted_returned_message="{}", tokens_in=10, tokens_out=5, model="claude-haiku-4-5",
            )
        self.client.force_login(self.user)

        data = self.client.get("/api/cost/insights_summary/").json()

        self.assertEqual(data["conversations_analyzed"], 6)
        transcripts_seen = mock_gen.call_args[0][0]
        self.assertEqual(len(transcripts_seen), 6)
        self.assertTrue(all("booster boxes" not in t for t in transcripts_seen))

    @patch(
        "cost_management.insights_views._generate_insights",
        return_value={
            "headline": "One Piece singles are the top request.",
            "top_requests": "One Piece singles, mostly.",
            "count": 4,
            "share_pct": 40,
            "examples": ["conv-1", "conv-2"],
            "unmet_needs": [
                {"gap": "Grading questions", "gap_type": "capability", "count": 2,
                 "summary": "Bot defers to email.", "examples": ["conv-3"]},
            ],
            "product_demand": [],
            "recommendations": [],
        },
    )
    def test_malformed_model_output_is_sanitized_not_passed_through(self, mock_gen):
        """report_insights tool-call arguments aren't schema-validated by the
        API, so a malformed generation (a list field emitted as a string)
        must not reach the response — the frontend calls .map() on these
        fields and a non-list crashes the whole /home page."""
        self._make_conversations(6, with_customer_text=2)
        self.client.force_login(self.user)

        response = self.client.get("/api/cost/insights_summary/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["top_requests"], [])
        self.assertEqual(
            data["unmet_needs"],
            [{"gap": "Grading questions", "gap_type": "capability", "count": 2,
              "summary": "Bot defers to email.", "examples": ["conv-3"]}],
        )
        # This fixture's shape is exactly what a truncated tool call produced
        # live (2026-09-17): a malformed list field accompanied by that list
        # item's own fields ("count", "share_pct", "examples") orphaned as
        # top-level siblings. Those must never leak into the API response.
        self.assertNotIn("count", data)
        self.assertNotIn("share_pct", data)
        self.assertNotIn("examples", data)

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    def test_second_call_within_the_hour_is_served_from_cache(self, mock_gen):
        self._make_conversations(6)
        self.client.force_login(self.user)

        first = self.client.get("/api/cost/insights_summary/").json()
        second = self.client.get("/api/cost/insights_summary/").json()

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["top_requests"], CANNED_INSIGHTS["top_requests"])
        mock_gen.assert_called_once()

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    def test_refresh_forces_regeneration(self, mock_gen):
        self._make_conversations(6)
        self.client.force_login(self.user)

        self.client.get("/api/cost/insights_summary/")
        self.client.get("/api/cost/insights_summary/?refresh=1")

        self.assertEqual(mock_gen.call_count, 2)

    @patch("cost_management.insights_views._generate_insights")
    def test_past_month_returns_stored_payload_without_calling_the_model(self, mock_gen):
        from cost_management.models import InsightsSnapshot
        past = (_now().date().replace(day=1) - timedelta(days=1)).replace(day=1)
        stored = {**CANNED_INSIGHTS, "month": past.strftime("%Y-%m"), "conversations_analyzed": 40}
        InsightsSnapshot.objects.create(month=past, payload=stored, conversations_analyzed=40)
        self.client.force_login(self.user)

        response = self.client.get(f"/api/cost/insights_summary/?month={past:%Y-%m}")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["headline"], CANNED_INSIGHTS["headline"])
        self.assertTrue(data["cached"])
        self.assertIn("available_months", data)
        mock_gen.assert_not_called()

    def test_past_month_with_no_data_reports_insufficient(self):
        self.client.force_login(self.user)

        response = self.client.get("/api/cost/insights_summary/?month=2020-01")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["insufficient_data"])
        self.assertIn("available_months", data)

    def test_available_months_span_from_the_first_conversation_to_now(self):
        first_dt = _now().replace(day=1)
        for _ in range(2):
            first_dt = (first_dt - timedelta(days=1)).replace(day=1)
        self._make_conversations(3, when=first_dt.replace(day=10), prefix="old")
        self._make_conversations(3, prefix="new")
        self.client.force_login(self.user)

        values = [m["value"] for m in self.client.get("/api/cost/insights_summary/").json()["available_months"]]

        self.assertEqual(values[0], _now().strftime("%Y-%m"))       # sorted newest-first
        self.assertEqual(values[-1], first_dt.strftime("%Y-%m"))
        self.assertGreaterEqual(len(values), 3)

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    def test_missing_past_month_with_data_is_generated_on_request(self, mock_gen):
        from cost_management.models import InsightsSnapshot
        prev_dt = (_now().replace(day=1) - timedelta(days=1)).replace(day=15)
        self._make_conversations(6, when=prev_dt, prefix="prev")
        self.client.force_login(self.user)

        data = self.client.get(
            f"/api/cost/insights_summary/?month={prev_dt.strftime('%Y-%m')}"
        ).json()

        self.assertEqual(data["headline"], CANNED_INSIGHTS["headline"])
        mock_gen.assert_called_once()
        self.assertTrue(
            InsightsSnapshot.objects.filter(month=prev_dt.date().replace(day=1)).exists()
        )

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    def test_previous_month_is_backfilled_on_a_current_month_generation(self, mock_gen):
        from cost_management.models import InsightsSnapshot
        current_start = _now().date().replace(day=1)
        prev_start = (current_start - timedelta(days=1)).replace(day=1)
        prev_when = _now().replace(day=1) - timedelta(days=1)
        self._make_conversations(6, prefix="cur")
        self._make_conversations(6, when=prev_when, prefix="prev")
        self.client.force_login(self.user)

        self.client.get("/api/cost/insights_summary/")

        self.assertTrue(InsightsSnapshot.objects.filter(month=prev_start).exists())

    @patch("cost_management.insights_views._generate_insights")
    def test_insufficient_data_makes_no_model_call(self, mock_gen):
        self._make_conversations(3)
        self.client.force_login(self.user)

        data = self.client.get("/api/cost/insights_summary/").json()

        self.assertTrue(data["insufficient_data"])
        self.assertIn("available_months", data)
        mock_gen.assert_not_called()

    @patch("cost_management.insights_views._generate_insights", side_effect=RuntimeError("boom"))
    def test_model_error_returns_200_with_error_field(self, mock_gen):
        from cost_management.models import InsightsSnapshot
        self._make_conversations(6)
        self.client.force_login(self.user)

        response = self.client.get("/api/cost/insights_summary/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["error"], "boom")
        self.assertIsNone(data["stale"])
        self.assertFalse(InsightsSnapshot.objects.exists())

    @patch(
        "cost_management.insights_views._generate_insights",
        return_value={"headline": "", "top_requests": [], "unmet_needs": [],
                      "product_demand": [], "recommendations": []},
    )
    def test_hollow_report_does_not_overwrite_a_good_stored_snapshot(self, mock_gen):
        """Observed live (2026-09-17): the model can complete normally (no
        exception, no malformed shape) and still return a report with no
        headline and no evidence in any list. That must not overwrite a
        previously-good stored snapshot -- degrade to the stale result
        instead, same as a real generation error."""
        from cost_management.insights_views import INSIGHTS_MAX_ATTEMPTS
        from cost_management.models import InsightsSnapshot

        month = _now().date().replace(day=1)
        InsightsSnapshot.objects.create(
            month=month, payload={**CANNED_INSIGHTS, "month": month.strftime("%Y-%m")},
            conversations_analyzed=6,
        )
        self._make_conversations(6)
        self.client.force_login(self.user)

        data = self.client.get("/api/cost/insights_summary/?refresh=1").json()

        self.assertIn("empty report", data["error"])
        self.assertEqual(data["stale"]["headline"], CANNED_INSIGHTS["headline"])
        snap = InsightsSnapshot.objects.get(month=month)
        self.assertEqual(snap.payload["headline"], CANNED_INSIGHTS["headline"])
        self.assertEqual(mock_gen.call_count, INSIGHTS_MAX_ATTEMPTS)

    @patch(
        "cost_management.insights_views._generate_insights",
        side_effect=[
            {"headline": "", "top_requests": [], "unmet_needs": [],
             "product_demand": [], "recommendations": []},
            dict(CANNED_INSIGHTS),
        ],
    )
    def test_retries_after_a_hollow_report_and_succeeds(self, mock_gen):
        """A hollow report on the first attempt shouldn't sink the whole
        request if a retry produces a real one -- observed live (2026-09-17)
        that the exact same prompt/transcripts can go either way."""
        self._make_conversations(6)
        self.client.force_login(self.user)

        data = self.client.get("/api/cost/insights_summary/").json()

        self.assertEqual(data["headline"], CANNED_INSIGHTS["headline"])
        self.assertEqual(mock_gen.call_count, 2)

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    def test_conversation_list_is_capped_and_flagged_sampled(self, mock_gen):
        self._make_conversations(205)
        self.client.force_login(self.user)

        data = self.client.get("/api/cost/insights_summary/").json()

        self.assertTrue(data["sampled"])
        transcripts_arg = mock_gen.call_args.args[0]
        self.assertEqual(len(transcripts_arg), 200)

    @override_settings(TESTING=False)
    @patch("cost_management.insights_views.threading.Thread")
    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    def test_stale_snapshot_is_served_immediately_while_refreshing_in_background(self, mock_gen, mock_thread):
        from cost_management.models import InsightsSnapshot
        current = _now().date().replace(day=1)
        stored = {**CANNED_INSIGHTS, "month": current.strftime("%Y-%m"),
                  "conversations_analyzed": 20, "headline": "old headline"}
        InsightsSnapshot.objects.create(month=current, payload=stored, conversations_analyzed=20)
        self._make_conversations(6)
        self.client.force_login(self.user)

        data = self.client.get("/api/cost/insights_summary/").json()

        self.assertTrue(data["regenerating"])
        self.assertEqual(data["headline"], "old headline")
        mock_thread.assert_called_once()
        mock_gen.assert_not_called()

    @override_settings(TESTING=False)
    @patch("cost_management.insights_views.threading.Thread")
    def test_first_ever_load_returns_generating_flag(self, mock_thread):
        self._make_conversations(6)
        self.client.force_login(self.user)

        data = self.client.get("/api/cost/insights_summary/").json()

        self.assertTrue(data["generating"])
        self.assertIn("available_months", data)
        mock_thread.assert_called_once()

    @patch("cost_management.insights_views._generate_insights")
    def test_product_demand_drops_one_off_requests(self, mock_gen):
        mock_gen.return_value = {
            **CANNED_INSIGHTS,
            "product_demand": [
                {"product": "OP17 Booster Box", "count": 4, "status": "out_of_stock", "examples": ["c1"]},
                {"product": "Darkrai VSTAR single", "count": 1, "status": "out_of_stock", "examples": ["c2"]},
                {"product": "The Mind board game", "count": 1, "status": "not_carried", "examples": ["c3"]},
            ],
        }
        self._make_conversations(6)
        self.client.force_login(self.user)

        data = self.client.get("/api/cost/insights_summary/").json()

        self.assertEqual([d["product"] for d in data["product_demand"]], ["OP17 Booster Box"])
        self.assertEqual(data["product_demand_one_offs"], 2)

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    def test_recommendations_pass_through_sorted_by_impact_then_evidence(self, mock_gen):
        self._make_conversations(6)
        self.client.force_login(self.user)

        data = self.client.get("/api/cost/insights_summary/").json()

        self.assertEqual(
            [r["title"] for r in data["recommendations"]],
            ["Load store policies", "Connect order-status lookup", "Tune catalog search"],
        )
        self.assertEqual(data["recommendations"][0]["examples"], ["conv-3"])

    @override_settings(TESTING=False)
    @patch("cost_management.insights_views.threading.Thread")
    def test_first_ever_load_returns_generating_flag_with_progress(self, mock_thread):
        self._make_conversations(6)
        self.client.force_login(self.user)

        data = self.client.get("/api/cost/insights_summary/").json()

        self.assertTrue(data["generating"])
        self.assertIn("progress", data)
        self.assertIn("percent", data["progress"])
        self.assertIn("stage", data["progress"])

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    def test_generate_insights_management_command(self, mock_gen):
        from django.core.management import call_command
        from cost_management.models import InsightsSnapshot
        self._make_conversations(6, with_customer_text=2)

        call_command("generate_insights")

        first_of_month = _now().date().replace(day=1)
        snap = InsightsSnapshot.objects.get(month=first_of_month)
        self.assertEqual(snap.conversations_analyzed, 6)
        self.assertEqual(snap.payload["headline"], CANNED_INSIGHTS["headline"])


def _cost_resp(*totals):
    return {"costs": [{"day": f"2026-08-{i + 1:02d}", "total_cost": t} for i, t in enumerate(totals)]}


def _tok_resp(*pairs, cache=None):
    resp = {"tokens": [
        {"day": f"2026-08-{i + 1:02d}", "input_tokens": a, "output_tokens": b}
        for i, (a, b) in enumerate(pairs)
    ]}
    if cache is not None:
        resp["cache"] = cache
    return resp


class MonthlyStatsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")
        self.this_month = _now().replace(day=1)
        self.prev_month = (self.this_month - timedelta(days=2)).replace(day=1)

    def tearDown(self):
        cache.clear()

    def _seed(self, n, when, tokens_in=1000, tokens_out=300, score=None,
              model="claude-haiku-4-5", prefix="c"):
        for i in range(n):
            chat = Chat.objects.create(
                chat_id=f"{prefix}-{when:%Y%m%d}-{i}", model=model,
                tokens_in=tokens_in, tokens_out=tokens_out, evaluation_score=score,
            )
            Chat.objects.filter(pk=chat.pk).update(timestamp=when)

    def _patch_adapter(self):
        # month M (current) totals 15.0 spend / 3000 in / 700 out; month P totals 10.0 / 800 / 200
        cur, prev = self.this_month.month, self.prev_month.month
        # rates_resp: _spend_for now passes the month's already-fetched
        # get_model_rates() response straight through to get_cost (see
        # AnthropicAdapter.get_cost's rates_resp param) instead of letting
        # get_cost derive its own -- accepted and ignored here since this
        # mock's canned response doesn't vary by rate.
        get_cost = MagicMock(side_effect=lambda year, month, key_ids=None, rates_resp=None:
                             _cost_resp(5.0, 7.0, 3.0) if month == cur else _cost_resp(4.0, 6.0))
        get_tokens = MagicMock(side_effect=lambda year, month, key_ids=None:
                               _tok_resp((1000, 300), (2000, 400)) if month == cur else _tok_resp((800, 200)))
        # Needed so _rates_for (called from _build_stats to cost-weight the
        # ENG-149/150 bot's share out of cost_pc's numerator) doesn't hit the
        # real network from every test in this class -- see _rates_for's
        # "returns {} on error or exception" contract, which is what an
        # unmocked call would otherwise silently fall back to.
        get_model_rates = MagicMock(return_value={"rates": {
            "claude-haiku-4-5": {"input": 0.000001, "output": 0.000005},
            "claude-sonnet-5": {"input": 0.000003, "output": 0.000015},
        }})
        p = patch.multiple("cost_management.views.llmprovider",
                           get_cost=get_cost, get_tokens=get_tokens, get_model_rates=get_model_rates)
        p.start()
        self.addCleanup(p.stop)
        return get_cost, get_tokens

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/cost/monthly_stats/").status_code, 401)

    def test_totals_and_month_over_month_deltas(self):
        self._patch_adapter()
        self._seed(3, self.this_month.replace(day=10))
        self._seed(1, self.this_month.replace(day=20))
        self._seed(2, self.prev_month.replace(day=15))
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/monthly_stats/").json()

        self.assertEqual(d["spend"]["total"], 15.0)
        self.assertEqual(d["spend"]["prev_total"], 10.0)
        self.assertEqual(d["spend"]["delta_pct"], 50.0)
        self.assertEqual(d["tokens"]["input"], 3000)
        self.assertEqual(d["tokens"]["output"], 700)
        self.assertEqual(d["conversations"]["total"], 4)
        self.assertEqual(d["conversations"]["prev_total"], 2)
        self.assertEqual(d["conversations"]["delta_pct"], 100.0)
        self.assertEqual(len(d["spend"]["daily"]), 3)
        self.assertEqual(len(d["conversations"]["daily"]), 2)
        self.assertEqual(d["conversations"]["busiest"]["count"], 3)
        self.assertEqual(d["per_conversation"]["cost"], round(15.0 / 4, 4))
        self.assertIn("labor_savings", d)
        self.assertEqual(d["labor_savings"]["labor_rate_hourly"], 18.0)
        self.assertEqual(d["labor_savings"]["estimated_labor_hours"], 0.3)
        self.assertIn("low_score_count", d)

    def test_likely_automated_chats_are_excluded_from_every_stat(self):
        """ENG-149/150: a flagged bot chat must not appear in the dashboard's
        conversation count, daily breakdown, busiest-day, or model mix --
        flag_automated_chats sets this field on exactly the chats that
        corrupted these numbers in prod. The cost/conversation numerator
        must ALSO exclude the bot's own logged spend: the bot hits the same
        chat API key as real shoppers, so billed spend this month still
        includes its traffic even though `convs` (the denominator) already
        excludes it. Dividing the full $15 by 3 real conversations would
        still overstate cost/conversation by the bot's share of logged
        spend -- 20 of these 23 chats are bot, same model, identical token
        counts, so 20/23 of the $15 is prorated out first."""
        self._patch_adapter()
        self._seed(3, self.this_month.replace(day=10))  # 3 real conversations
        for i in range(20):
            chat = Chat.objects.create(
                chat_id=f"bot-{i}", model="claude-haiku-4-5",
                tokens_in=1000, tokens_out=300, likely_automated=True,
            )
            Chat.objects.filter(pk=chat.pk).update(timestamp=self.this_month.replace(day=10))
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/monthly_stats/").json()

        self.assertEqual(d["conversations"]["total"], 3)
        self.assertEqual(d["conversations"]["busiest"]["count"], 3)
        self.assertEqual(d["per_conversation"]["cost"], round(15.0 * (3 / 23) / 3, 4))
        self.assertEqual(d["per_conversation"]["billed_spend"], 15.0)
        self.assertAlmostEqual(d["per_conversation"]["spend_excl_bot"], round(15.0 * (3 / 23), 2), places=2)
        self.assertEqual(d["per_conversation"]["bot_share_pct"], round(20 / 23 * 100, 1))
        self.assertEqual(sum(m["conversations"] for m in d["model_mix"]), 3)
        # The bot chats still show up in the daily breakdown's bot_count --
        # for the stacked bar chart -- without inflating `count` (real).
        day10 = next(day for day in d["conversations"]["daily"] if day["count"] or day["bot_count"])
        self.assertEqual(day10["count"], 3)
        self.assertEqual(day10["bot_count"], 20)

    def test_daily_breakdown_splits_real_and_bot_counts_per_day(self):
        """The 'Conversations per day' stacked bar needs both series -- a day
        with only bot traffic must still appear (count=0, bot_count>0)."""
        self._patch_adapter()
        self._seed(2, self.this_month.replace(day=5))  # real only
        bot_only_chat = Chat.objects.create(chat_id="bot-only", model="claude-haiku-4-5", likely_automated=True)
        Chat.objects.filter(pk=bot_only_chat.pk).update(timestamp=self.this_month.replace(day=6))
        self.client.force_login(self.user)

        daily = self.client.get("/api/cost/monthly_stats/").json()["conversations"]["daily"]
        by_day = {row["day"]: row for row in daily}

        day5 = self.this_month.replace(day=5).date().isoformat()
        day6 = self.this_month.replace(day=6).date().isoformat()
        self.assertEqual(by_day[day5], {"day": day5, "count": 2, "bot_count": 0})
        self.assertEqual(by_day[day6], {"day": day6, "count": 0, "bot_count": 1})

    def test_eval_coverage(self):
        self._patch_adapter()
        self._seed(2, self.this_month.replace(day=5), score=80)
        self._seed(1, self.this_month.replace(day=6), score=90)
        self._seed(1, self.this_month.replace(day=7))  # unscored
        self.client.force_login(self.user)

        ev = self.client.get("/api/cost/monthly_stats/").json()["eval_score"]

        self.assertEqual(ev["scored"], 3)
        self.assertEqual(ev["total"], 4)
        self.assertEqual(ev["coverage_pct"], 75.0)
        self.assertIsNotNone(ev["avg"])

    def test_cost_source_failure_degrades_but_keeps_db_sections(self):
        p = patch.multiple(
            "cost_management.views.llmprovider",
            get_cost=MagicMock(return_value={"error": "boom"}),
            get_tokens=MagicMock(return_value={"error": "boom"}),
        )
        p.start()
        self.addCleanup(p.stop)
        self._seed(3, self.this_month.replace(day=9))
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/monthly_stats/").json()

        self.assertEqual(d["cost_source_error"], "boom")
        self.assertIsNone(d["spend"]["total"])
        self.assertIsNone(d["per_conversation"]["cost"])
        self.assertEqual(d["conversations"]["total"], 3)

    def test_projection_only_for_current_month(self):
        self._patch_adapter()
        self._seed(3, self.this_month.replace(day=9))
        self._seed(3, self.prev_month.replace(day=9))
        self.client.force_login(self.user)

        current = self.client.get("/api/cost/monthly_stats/").json()
        past = self.client.get(f"/api/cost/monthly_stats/?month={self.prev_month:%Y-%m}").json()

        self.assertIsNotNone(current["spend"]["projected_month_end"])
        self.assertIsNone(past["spend"]["projected_month_end"])
        self.assertTrue(current["is_current"])
        self.assertFalse(past["is_current"])

    def test_caches_and_refresh_bypasses(self):
        get_cost, _ = self._patch_adapter()
        self._seed(3, self.this_month.replace(day=9))
        self.client.force_login(self.user)

        self.client.get("/api/cost/monthly_stats/")
        calls_after_first = get_cost.call_count
        cached = self.client.get("/api/cost/monthly_stats/").json()
        self.assertTrue(cached["cached"])
        self.assertEqual(get_cost.call_count, calls_after_first)

        self.client.get("/api/cost/monthly_stats/?refresh=1")
        self.assertGreater(get_cost.call_count, calls_after_first)

    def test_model_mix(self):
        self._patch_adapter()
        self._seed(3, self.this_month.replace(day=8), model="claude-haiku-4-5", prefix="h")
        self._seed(1, self.this_month.replace(day=8), model="claude-sonnet-5", prefix="s")
        self.client.force_login(self.user)

        mix = self.client.get("/api/cost/monthly_stats/").json()["model_mix"]

        self.assertEqual(mix[0]["model"], "claude-haiku-4-5")
        self.assertEqual(mix[0]["conversations"], 3)
        self.assertEqual(mix[0]["share_pct"], 75.0)

    def test_invalid_month_is_400(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/cost/monthly_stats/?month=nope").status_code, 400)

    def test_month_with_no_data(self):
        self._patch_adapter()
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/monthly_stats/?month=2020-01").json()

        self.assertEqual(d["conversations"]["total"], 0)
        self.assertIsNone(d["per_conversation"]["cost"])
        self.assertEqual(d["model_mix"], [])

    def test_workspace_id_reported_when_configured(self):
        self._patch_adapter()
        self.client.force_login(self.user)

        with patch.dict('os.environ', {'ANTHROPIC_WORKSPACE_ID': 'wrkspc_target'}):
            d = self.client.get("/api/cost/monthly_stats/").json()
        self.assertEqual(d["workspace_id"], "wrkspc_target")

        cache.clear()
        with patch.dict('os.environ', {}, clear=False):
            os.environ.pop('ANTHROPIC_WORKSPACE_ID', None)
            d = self.client.get("/api/cost/monthly_stats/").json()
        self.assertIsNone(d["workspace_id"])

    def test_spend_and_tokens_are_scoped_to_chat_api_key_ids(self):
        """cost_pc's numerator (and the tokens/cache-hit-rate KPIs alongside
        it) must be scoped to the chat surface specifically, not every key
        this app has ever issued (ANTHROPIC_APP_API_KEY_IDS also covers the
        AI Search Curator/narrative/report surfaces since ENG-147's 3-way
        key split) -- otherwise growth on those surfaces inflates
        cost/conversation with no matching increase in the (chat-only)
        conversation count."""
        get_cost, get_tokens = self._patch_adapter()
        self._seed(3, self.this_month.replace(day=10))
        self.client.force_login(self.user)

        with patch.dict('os.environ', {
            'ANTHROPIC_APP_API_KEY_IDS': 'apikey_chat,apikey_search,apikey_report',
            'ANTHROPIC_CHAT_API_KEY_IDS': 'apikey_chat',
        }):
            self.client.get("/api/cost/monthly_stats/")

        for call in get_cost.call_args_list:
            self.assertEqual(call.kwargs.get("key_ids"), ["apikey_chat"])
        for call in get_tokens.call_args_list:
            self.assertEqual(call.kwargs.get("key_ids"), ["apikey_chat"])


class MonthlyStatsCacheHitRateTests(TestCase):
    """ENG-148: the store-owner-facing "how well is caching doing" metric --
    surfaced on monthly_stats so it renders on the same home dashboard as
    spend/tokens, not buried in a separate call."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")
        self.this_month = _now().replace(day=1)

    def tearDown(self):
        cache.clear()

    def _patch_adapter(self, cache_info):
        get_cost = MagicMock(return_value=_cost_resp(5.0))
        get_tokens = MagicMock(return_value=_tok_resp((1000, 300), cache=cache_info))
        # _build_stats now fetches rates (to prorate the bot's spend share
        # out of cost_pc) whenever spend isn't None -- mock it so these
        # tests don't hit the real network for a KPI they don't assert on.
        get_model_rates = MagicMock(return_value={"rates": {}})
        p = patch.multiple(
            "cost_management.views.llmprovider",
            get_cost=get_cost, get_tokens=get_tokens, get_model_rates=get_model_rates,
        )
        p.start()
        self.addCleanup(p.stop)

    def test_reports_cache_hit_rate_and_token_breakdown(self):
        self._patch_adapter({"creation_tokens": 500, "read_tokens": 4500, "hit_rate": 0.9})
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/monthly_stats/").json()

        self.assertEqual(d["tokens"]["cache_creation"], 500)
        self.assertEqual(d["tokens"]["cache_read"], 4500)
        self.assertEqual(d["tokens"]["cache_hit_rate"], 0.9)

    def test_hit_rate_none_when_adapter_reports_none(self):
        self._patch_adapter({"creation_tokens": 0, "read_tokens": 0, "hit_rate": None})
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/monthly_stats/").json()

        self.assertIsNone(d["tokens"]["cache_hit_rate"])

    def test_degrades_gracefully_when_adapter_omits_cache_entirely(self):
        """An older/unpatched adapter response has no "cache" key at all --
        must not break the whole monthly_stats response."""
        get_cost = MagicMock(return_value=_cost_resp(5.0))
        get_tokens = MagicMock(return_value=_tok_resp((1000, 300)))  # no cache=...
        get_model_rates = MagicMock(return_value={"rates": {}})
        p = patch.multiple(
            "cost_management.views.llmprovider",
            get_cost=get_cost, get_tokens=get_tokens, get_model_rates=get_model_rates,
        )
        p.start()
        self.addCleanup(p.stop)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/monthly_stats/").json()

        self.assertIsNone(d["tokens"]["cache_hit_rate"])
        self.assertEqual(d["tokens"]["cache_creation"], 0)
        self.assertEqual(d["tokens"]["cache_read"], 0)


class MonthlyStatsLifetimeTests(TestCase):
    def setUp(self):
        from datetime import datetime
        from django.utils import timezone
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(self.user)
        self.jun_2026 = timezone.make_aware(datetime(2026, 6, 15, 12, 0, 0))
        self.jul_2026 = timezone.make_aware(datetime(2026, 7, 10, 12, 0, 0))
        self.pre_june = timezone.make_aware(datetime(2026, 5, 20, 12, 0, 0))

    def tearDown(self):
        cache.clear()

    def _patch_adapter(self, spend=10.0, tokens=(1000, 300)):
        get_cost = MagicMock(return_value=_cost_resp(spend))
        get_tokens = MagicMock(return_value=_tok_resp(tokens, cache={"creation_tokens": 100, "read_tokens": 400}))
        get_model_rates = MagicMock(return_value={"rates": {"claude-haiku-4-5": {"input": 1e-6, "output": 5e-6}}})
        get_usage_by_key = MagicMock(return_value={
            "keys": [
                {
                    "api_key_id": "key_1",
                    "name": "Chatbot Key",
                    "input_tokens": 500,
                    "output_tokens": 150,
                    "by_model": {"claude-haiku-4-5": {"uncached_input_tokens": 500, "output_tokens": 150}}
                }
            ],
            "workspace_id": "ws_123"
        })
        p = patch.multiple(
            "cost_management.views.llmprovider",
            get_cost=get_cost,
            get_tokens=get_tokens,
            get_model_rates=get_model_rates,
            get_usage_by_key=get_usage_by_key,
        )
        p.start()
        self.addCleanup(p.stop)

    def test_monthly_stats_lifetime_aggregates_and_excludes_pre_june(self):
        self._patch_adapter()

        # Chat before June 1, 2026 - must be excluded
        c_old = Chat.objects.create(chat_id="chat-old", model="claude-haiku-4-5", evaluation_score=40)
        Chat.objects.filter(pk=c_old.pk).update(timestamp=self.pre_june)

        # Chat in June 2026
        c_jun = Chat.objects.create(chat_id="chat-jun", model="claude-haiku-4-5", evaluation_score=90)
        Chat.objects.filter(pk=c_jun.pk).update(timestamp=self.jun_2026)

        # Chat in July 2026
        c_jul = Chat.objects.create(chat_id="chat-jul", model="claude-haiku-4-5", evaluation_score=100)
        Chat.objects.filter(pk=c_jul.pk).update(timestamp=self.jul_2026)

        resp = self.client.get("/api/cost/monthly_stats/?month=lifetime")
        self.assertEqual(resp.status_code, 200)
        d = resp.json()

        self.assertTrue(d.get("is_lifetime"))
        self.assertEqual(d["conversations"]["total"], 2)  # only jun and jul
        self.assertEqual(d["eval_score"]["avg"], 95.0)    # (90 + 100) / 2
        self.assertEqual(d["eval_score"]["scored"], 2)
        self.assertEqual(d["low_score_count"], 0)
        self.assertGreater(d["spend"]["total"], 0)
        self.assertGreater(d["tokens"]["input"], 0)

    def test_usage_by_key_lifetime(self):
        self._patch_adapter()
        resp = self.client.get("/api/cost/get_usage_by_key/?month=lifetime")
        self.assertEqual(resp.status_code, 200)
        d = resp.json()
        self.assertTrue(d.get("is_lifetime"))
        self.assertTrue(len(d.get("keys", [])) >= 1)
        self.assertEqual(d["keys"][0]["api_key_id"], "key_1")

    def test_cost_reconciliation_lifetime(self):
        self._patch_adapter()
        resp = self.client.get("/api/cost/cost_reconciliation/?month=lifetime")
        self.assertEqual(resp.status_code, 200)
        d = resp.json()
        self.assertTrue(d.get("is_lifetime"))
        self.assertIsNotNone(d.get("billed_spend"))

    def test_cache_economics_lifetime(self):
        self._patch_adapter()
        resp = self.client.get("/api/cost/cache_economics/?month=lifetime")
        self.assertEqual(resp.status_code, 200)
        d = resp.json()
        self.assertTrue(d.get("is_lifetime"))
        self.assertIn("verdict", d)

    def test_insights_summary_lifetime(self):
        from datetime import datetime
        from .models import InsightsSnapshot
        # Create a snapshot in June 2026
        InsightsSnapshot.objects.create(
            month=datetime(2026, 6, 1).date(),
            payload={
                "product_demand": [{"product": "Charizard ex", "count": 5, "status": "out_of_stock", "examples": ["chat-1"]}],
                "top_requests": [{"topic": "Order status", "count": 10, "examples": ["chat-1"]}],
                "unmet_needs": [{"gap": "Return policy", "gap_type": "policy", "count": 3, "summary": "Unclear terms", "examples": ["chat-1"]}],
                "recommendations": [{"title": "Clarify returns", "impact": "high", "effort": "low", "detail": "Add return policy FAQ", "evidence_count": 3}],
            },
            conversations_analyzed=10
        )
        resp = self.client.get("/api/cost/insights_summary/?month=lifetime")
        self.assertEqual(resp.status_code, 200)
        d = resp.json()
        self.assertTrue(d.get("is_lifetime"))
        self.assertTrue(len(d.get("product_demand", [])) >= 1)
        self.assertEqual(d["product_demand"][0]["product"], "Charizard ex")


class RateForHelperTests(TestCase):
    """_rate_for mirrors the frontend's getModelRate (chatSummary/pricing.js)
    -- longest-prefix match, since Chat/Message `model` values carry a dated
    snapshot suffix (e.g. "claude-haiku-4-5-20251001") while Anthropic's
    cost/usage reports key rates by a shorter model string."""

    def test_matches_exact_model(self):
        from .stats_views import _rate_for
        rates = {"claude-haiku-4-5": {"input": 1e-6}}
        self.assertEqual(_rate_for(rates, "claude-haiku-4-5"), {"input": 1e-6})

    def test_matches_dated_suffix_via_prefix(self):
        from .stats_views import _rate_for
        rates = {"claude-haiku-4-5": {"input": 1e-6}, "claude-sonnet-5": {"input": 3e-6}}
        self.assertEqual(_rate_for(rates, "claude-haiku-4-5-20251001"), {"input": 1e-6})

    def test_prefers_the_longest_matching_prefix(self):
        rates = {
            "claude-haiku-4-5": {"input": 1e-6},
            "claude-haiku-4-5-2025": {"input": 9e-6},
        }
        from .stats_views import _rate_for
        self.assertEqual(_rate_for(rates, "claude-haiku-4-5-20251001"), {"input": 9e-6})

    def test_returns_none_when_no_prefix_matches(self):
        from .stats_views import _rate_for
        self.assertIsNone(_rate_for({"claude-sonnet-5": {"input": 3e-6}}, "claude-haiku-4-5-20251001"))

    def test_returns_none_for_missing_model_or_empty_rates(self):
        from .stats_views import _rate_for
        self.assertIsNone(_rate_for({"claude-haiku-4-5": {"input": 1e-6}}, None))
        self.assertIsNone(_rate_for({}, "claude-haiku-4-5"))
        self.assertIsNone(_rate_for(None, "claude-haiku-4-5"))


class RatesForHelperTests(TestCase):
    """_rates_for must never raise -- a missing/unmocked/erroring adapter
    call has to degrade the dashboard, not break it."""

    def test_returns_rates_dict_on_success(self):
        from .stats_views import _rates_for
        get_model_rates = MagicMock(return_value={"rates": {"claude-haiku-4-5": {"input": 1e-6}}})
        p = patch.multiple("cost_management.views.llmprovider", get_model_rates=get_model_rates)
        p.start()
        self.addCleanup(p.stop)

        self.assertEqual(_rates_for(_now().replace(day=1)), {"claude-haiku-4-5": {"input": 1e-6}})

    def test_returns_empty_dict_on_error_response(self):
        from .stats_views import _rates_for
        get_model_rates = MagicMock(return_value={"error": "boom"})
        p = patch.multiple("cost_management.views.llmprovider", get_model_rates=get_model_rates)
        p.start()
        self.addCleanup(p.stop)

        self.assertEqual(_rates_for(_now().replace(day=1)), {})

    def test_returns_empty_dict_on_exception(self):
        """The real regression this guards: MonthlyStatsTests' shared
        _patch_adapter didn't mock get_model_rates until this fix, so every
        cost_pc-touching test would have hit the real adapter (and the
        network) the moment _build_stats started calling it."""
        from .stats_views import _rates_for
        get_model_rates = MagicMock(side_effect=requests.exceptions.ConnectionError("no network"))
        p = patch.multiple("cost_management.views.llmprovider", get_model_rates=get_model_rates)
        p.start()
        self.addCleanup(p.stop)

        self.assertEqual(_rates_for(_now().replace(day=1)), {})


class BotSpendShareTests(TestCase):
    """cost_pc's numerator must exclude the ENG-149/150 bot's own logged
    spend -- cost-weighted, not token-weighted, since cache reads bill at a
    steep discount and cache writes at a premium (see _bot_spend_share)."""

    def setUp(self):
        self.month_start = _now().replace(day=1)

    def _chat(self, chat_id, model, tokens_in, tokens_out, likely_automated):
        chat = Chat.objects.create(
            chat_id=chat_id, model=model, tokens_in=tokens_in, tokens_out=tokens_out,
            likely_automated=likely_automated,
        )
        Chat.objects.filter(pk=chat.pk).update(timestamp=self.month_start.replace(day=10))
        return chat

    def _message(self, chat, cache_creation_tokens=0, cache_read_tokens=0):
        Message.objects.create(
            chat=chat, content="", llm_formatted_message="", returned_content="",
            llm_formatted_returned_message="", tokens_in=chat.tokens_in, tokens_out=chat.tokens_out,
            cache_creation_tokens=cache_creation_tokens, cache_read_tokens=cache_read_tokens,
            model=chat.model,
        )

    def test_logged_spend_split_prices_chat_and_message_tokens_at_their_own_rate(self):
        from .stats_views import _logged_spend_split
        real = self._chat("real-1", "claude-haiku-4-5", 1_000_000, 500_000, False)
        self._message(real, cache_creation_tokens=100_000)
        rates = {"claude-haiku-4-5": {
            "input": 0.000001, "output": 0.000005, "cache_creation": 0.00000125, "cache_read": 0.0000001,
        }}

        split = _logged_spend_split(self.month_start, rates)

        # 1,000,000*1e-6 + 500,000*5e-6 + 100,000*1.25e-6 = 1.0 + 2.5 + 0.125
        self.assertAlmostEqual(split["real"], 1.0 + 2.5 + 0.125, places=6)
        self.assertEqual(split["bot"], 0.0)

    def test_missing_rate_for_a_model_contributes_zero_not_an_error(self):
        from .stats_views import _logged_spend_split
        self._chat("real-1", "some-unpriced-model", 1000, 300, False)

        split = _logged_spend_split(self.month_start, {"claude-haiku-4-5": {"input": 1e-6}})

        self.assertEqual(split, {"real": 0.0, "bot": 0.0})

    def test_bot_share_is_cost_weighted_not_token_weighted(self):
        """A bot chat with 100x a real chat's own token volume, almost
        entirely cheap cache reads, must not claim a ~99% (token-weighted)
        share of logged spend -- only its actual, much smaller cost share."""
        from .stats_views import _bot_spend_share
        real = self._chat("real-1", "claude-haiku-4-5", 1_000_000, 500_000, False)
        self._message(real)
        bot = self._chat("bot-1", "claude-haiku-4-5", 1_000_000, 500_000, True)
        self._message(bot, cache_read_tokens=100_000_000)  # 100x the real chat's own token volume
        rates = {"claude-haiku-4-5": {
            "input": 0.000001, "output": 0.000005, "cache_read": 0.0000001,  # 10x cheaper than input
        }}

        share = _bot_spend_share(self.month_start, rates)

        real_cost = 1_000_000 * 0.000001 + 500_000 * 0.000005          # 1.0 + 2.5 = 3.5
        bot_cost = real_cost + 100_000_000 * 0.0000001                  # 3.5 + 10.0 = 13.5
        expected_share = bot_cost / (real_cost + bot_cost)              # 13.5 / 17.0
        self.assertAlmostEqual(share, expected_share, places=6)
        # A naive token-weighted share would be ~99% (100M of ~101.5M total
        # tokens) -- the real, cost-weighted share must land far below that.
        self.assertLess(share, 0.9)

    def test_bot_spend_share_none_when_no_rates(self):
        from .stats_views import _bot_spend_share
        self._chat("bot-1", "claude-haiku-4-5", 1000, 300, True)
        self.assertIsNone(_bot_spend_share(self.month_start, {}))

    def test_bot_spend_share_none_when_no_logged_tokens_are_priceable(self):
        """Degrades to None, not 0.0, so _real_spend_for leaves the billed
        figure untouched rather than claiming a confirmed zero bot share."""
        from .stats_views import _bot_spend_share
        self._chat("bot-1", "some-unpriced-model", 1000, 300, True)
        rates = {"claude-haiku-4-5": {"input": 0.000001, "output": 0.000005}}
        self.assertIsNone(_bot_spend_share(self.month_start, rates))

    def test_real_spend_for_prorates_out_the_bot_share(self):
        from .stats_views import _real_spend_for
        self._chat("real-1", "claude-haiku-4-5", 1_000_000, 500_000, False)
        for i in range(3):
            self._chat(f"bot-{i}", "claude-haiku-4-5", 1_000_000, 500_000, True)
        rates = {"claude-haiku-4-5": {"input": 0.000001, "output": 0.000005}}

        prorated, share = _real_spend_for(20.0, self.month_start, rates)

        self.assertAlmostEqual(share, 3 / 4, places=6)  # 3 of 4 chats, identical cost each, are bot
        self.assertAlmostEqual(prorated, 20.0 * (1 / 4), places=6)

    def test_real_spend_for_passes_through_billed_spend_when_share_is_none(self):
        from .stats_views import _real_spend_for
        prorated, share = _real_spend_for(20.0, self.month_start, {})
        self.assertEqual(prorated, 20.0)
        self.assertIsNone(share)

    def test_real_spend_for_returns_none_when_spend_is_none(self):
        from .stats_views import _real_spend_for
        prorated, share = _real_spend_for(None, self.month_start, {"claude-haiku-4-5": {"input": 1e-6}})
        self.assertIsNone(prorated)
        self.assertIsNone(share)


class CostReconciliationEndpointTests(TestCase):
    """Billed spend vs. summed per-chat/per-message logged estimates -- the
    gap is chat-key spend with no matching Message row (rejected probes,
    failed requests, unlogged calls)."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")
        self.this_month = _now().replace(day=1)

    def tearDown(self):
        cache.clear()

    def _patch_adapter(self, spend=15.0, rates=None):
        get_cost = MagicMock(return_value=_cost_resp(spend))
        get_model_rates = MagicMock(return_value={"rates": rates if rates is not None else {
            "claude-haiku-4-5": {"input": 0.000001, "output": 0.000005},
        }})
        p = patch.multiple("cost_management.views.llmprovider", get_cost=get_cost, get_model_rates=get_model_rates)
        p.start()
        self.addCleanup(p.stop)
        return get_cost, get_model_rates

    def _chat(self, chat_id, tokens_in, tokens_out, likely_automated=False, model="claude-haiku-4-5"):
        chat = Chat.objects.create(
            chat_id=chat_id, model=model, tokens_in=tokens_in, tokens_out=tokens_out,
            likely_automated=likely_automated,
        )
        Chat.objects.filter(pk=chat.pk).update(timestamp=self.this_month.replace(day=10))
        return chat

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/cost/cost_reconciliation/").status_code, 401)

    def test_billed_vs_logged_with_unaccounted_remainder(self):
        self._patch_adapter(spend=15.0)
        self._chat("c-1", 1_000_000, 500_000)  # logged: 1_000_000*1e-6 + 500_000*5e-6 = 1.0 + 2.5 = 3.5
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/cost_reconciliation/").json()

        self.assertEqual(d["billed_spend"], 15.0)
        self.assertEqual(d["logged_spend"], 3.5)
        self.assertEqual(d["unaccounted"], round(15.0 - 3.5, 2))

    def test_chat_scope_is_app_wide_when_env_var_unset(self):
        self._patch_adapter()
        self.client.force_login(self.user)
        with patch.dict('os.environ', {}, clear=False):
            os.environ.pop('ANTHROPIC_CHAT_API_KEY_IDS', None)
            d = self.client.get("/api/cost/cost_reconciliation/").json()
        self.assertTrue(d["chat_scope_is_app_wide"])

    def test_chat_scope_is_not_app_wide_when_chat_key_ids_configured(self):
        self._patch_adapter()
        self.client.force_login(self.user)
        with patch.dict('os.environ', {'ANTHROPIC_CHAT_API_KEY_IDS': 'apikey_chat'}):
            d = self.client.get("/api/cost/cost_reconciliation/").json()
        self.assertFalse(d["chat_scope_is_app_wide"])

    def test_caches_and_refresh_bypasses(self):
        get_cost, _ = self._patch_adapter()
        self.client.force_login(self.user)

        self.client.get("/api/cost/cost_reconciliation/")
        calls_after_first = get_cost.call_count
        cached = self.client.get("/api/cost/cost_reconciliation/").json()
        self.assertTrue(cached["cached"])
        self.assertEqual(get_cost.call_count, calls_after_first)

        self.client.get("/api/cost/cost_reconciliation/?refresh=1")
        self.assertGreater(get_cost.call_count, calls_after_first)

    def test_invalid_month_is_400(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/cost/cost_reconciliation/?month=nope").status_code, 400)


class CacheEconomicsEndpointTests(TestCase):
    """Reads-per-write reuse ratio plus estimated $ spent on cached input vs.
    a baseline of pricing that same input as if none of it were cached --
    the "no caching at all" comparison, not a different-TTL one. Scoped to
    chat_api_key_ids() like cost_reconciliation, via get_usage_by_key's
    per-key by_model breakdown (that adapter method itself stays unscoped)."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")

    def tearDown(self):
        cache.clear()

    def _patch_adapter(self, usage_return=None, rates_return=None):
        get_usage_by_key = MagicMock(return_value=usage_return if usage_return is not None else {"keys": []})
        get_model_rates = MagicMock(return_value=rates_return if rates_return is not None else {"rates": {}})
        p = patch.multiple(
            "cost_management.views.llmprovider",
            get_usage_by_key=get_usage_by_key,
            get_model_rates=get_model_rates,
        )
        p.start()
        self.addCleanup(p.stop)
        return get_usage_by_key, get_model_rates

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/cost/cache_economics/").status_code, 401)

    def test_reads_per_write_and_savings_vs_uncached_baseline(self):
        self._patch_adapter(
            usage_return={"keys": [{
                "api_key_id": "apikey_chat", "name": "prod-shopify-chatbot",
                "input_tokens": 1_000_100, "output_tokens": 0,
                "by_model": {"claude-haiku-4-5": {
                    "uncached_input_tokens": 100, "output_tokens": 0,
                    "cache_creation_tokens": 1_000, "cache_read_tokens": 999_000,
                }},
            }]},
            rates_return={"rates": {"claude-haiku-4-5": {
                "input": 0.000001, "cache_creation": 0.00000125, "cache_read": 0.0000001,
            }}},
        )
        self.client.force_login(self.user)

        with patch.dict('os.environ', {'ANTHROPIC_CHAT_API_KEY_IDS': 'apikey_chat'}):
            d = self.client.get("/api/cost/cache_economics/").json()

        self.assertEqual(d["cache_read_tokens"], 999_000)
        self.assertEqual(d["cache_creation_tokens"], 1_000)
        self.assertEqual(d["reads_per_write"], 999.0)
        # actual: 100*0.000001 + 1000*0.00000125 + 999000*0.0000001 = 0.0001 + 0.00125 + 0.0999
        self.assertEqual(d["actual_cost"], round(0.0001 + 0.00125 + 0.0999, 2))
        # baseline: all 1_000_100 tokens billed at plain input rate
        self.assertEqual(d["baseline_cost"], round(1_000_100 * 0.000001, 2))
        self.assertEqual(d["savings"], round(d["baseline_cost"] - d["actual_cost"], 2))
        self.assertEqual(d["verdict"], "helping")
        # investment = 1000*(0.00000125-0.000001) = 0.00025; return = 999000*(0.000001-0.0000001) = 0.8991
        self.assertEqual(d["roi_multiple"], round(0.8991 / 0.00025, 2))

    def test_hurting_verdict_when_write_premium_exceeds_read_discount(self):
        """A cache write premium that isn't earned back by enough cheap reads
        must show as costing money, not as a positive-looking ratio -- this
        is exactly the scenario the plain-language verdict exists to flag."""
        self._patch_adapter(
            usage_return={"keys": [{
                "api_key_id": "apikey_chat", "name": "chat",
                "input_tokens": 1_001_000, "output_tokens": 0,
                "by_model": {"m": {
                    "uncached_input_tokens": 0, "output_tokens": 0,
                    "cache_creation_tokens": 1_000_000, "cache_read_tokens": 1_000,
                }},
            }]},
            rates_return={"rates": {"m": {
                "input": 0.000001, "cache_creation": 0.000002, "cache_read": 0.0000005,
            }}},
        )
        self.client.force_login(self.user)

        with patch.dict('os.environ', {'ANTHROPIC_CHAT_API_KEY_IDS': 'apikey_chat'}):
            d = self.client.get("/api/cost/cache_economics/").json()

        self.assertLess(d["savings"], 0)
        self.assertEqual(d["verdict"], "hurting")
        self.assertLess(d["roi_multiple"], 1)

    def test_key_outside_chat_scope_is_excluded(self):
        """A key outside chat_api_key_ids() (e.g. AI Search Curator's) must
        not contribute its cache tokens -- get_usage_by_key itself stays
        unscoped, so this endpoint must filter it, mirroring usage_by_key's
        own app_api_key_ids() filter one layer up."""
        self._patch_adapter(
            usage_return={"keys": [
                {"api_key_id": "apikey_chat", "name": "chat",
                 "input_tokens": 100, "output_tokens": 0,
                 "by_model": {"m": {"uncached_input_tokens": 0, "output_tokens": 0,
                                     "cache_creation_tokens": 10, "cache_read_tokens": 90}}},
                {"api_key_id": "apikey_curator", "name": "ai-search-curator",
                 "input_tokens": 10_000, "output_tokens": 0,
                 "by_model": {"m": {"uncached_input_tokens": 0, "output_tokens": 0,
                                     "cache_creation_tokens": 5_000, "cache_read_tokens": 5_000}}},
            ]},
            rates_return={"rates": {"m": {"input": 0.000001, "cache_creation": 0.00000125, "cache_read": 0.0000001}}},
        )
        self.client.force_login(self.user)

        with patch.dict('os.environ', {'ANTHROPIC_CHAT_API_KEY_IDS': 'apikey_chat'}):
            d = self.client.get("/api/cost/cache_economics/").json()

        self.assertEqual(d["cache_creation_tokens"], 10)
        self.assertEqual(d["cache_read_tokens"], 90)

    def test_no_writes_yields_none_ratio_not_error(self):
        self._patch_adapter(usage_return={"keys": []})
        self.client.force_login(self.user)
        d = self.client.get("/api/cost/cache_economics/").json()
        self.assertIsNone(d["reads_per_write"])
        self.assertIsNone(d["actual_cost"])
        self.assertIsNone(d["savings"])
        self.assertEqual(d["verdict"], "no_data")
        self.assertIsNone(d["roi_multiple"])

    def test_missing_rate_for_a_model_contributes_zero_not_an_error(self):
        self._patch_adapter(
            usage_return={"keys": [{
                "api_key_id": "apikey_chat", "name": "chat",
                "input_tokens": 100, "output_tokens": 0,
                "by_model": {"some-unpriced-model": {
                    "uncached_input_tokens": 0, "output_tokens": 0,
                    "cache_creation_tokens": 10, "cache_read_tokens": 90,
                }},
            }]},
            rates_return={"rates": {}},
        )
        self.client.force_login(self.user)

        with patch.dict('os.environ', {'ANTHROPIC_CHAT_API_KEY_IDS': 'apikey_chat'}):
            d = self.client.get("/api/cost/cache_economics/").json()

        self.assertEqual(d["reads_per_write"], 9.0)  # token ratio survives missing rates
        self.assertIsNone(d["actual_cost"])
        self.assertIsNone(d["savings"])
        self.assertEqual(d["verdict"], "no_data")
        self.assertIsNone(d["roi_multiple"])

    def test_chat_scope_is_app_wide_when_env_var_unset(self):
        self._patch_adapter()
        self.client.force_login(self.user)
        with patch.dict('os.environ', {}, clear=False):
            os.environ.pop('ANTHROPIC_CHAT_API_KEY_IDS', None)
            d = self.client.get("/api/cost/cache_economics/").json()
        self.assertTrue(d["chat_scope_is_app_wide"])

    def test_usage_source_error_yields_empty_buckets_with_error(self):
        self._patch_adapter(usage_return={"error": "boom"})
        self.client.force_login(self.user)
        d = self.client.get("/api/cost/cache_economics/").json()
        self.assertEqual(d["cost_source_error"], "boom")
        self.assertIsNone(d["reads_per_write"])

    def test_caches_and_refresh_bypasses(self):
        get_usage_by_key, _ = self._patch_adapter()
        self.client.force_login(self.user)

        self.client.get("/api/cost/cache_economics/")
        calls_after_first = get_usage_by_key.call_count
        cached = self.client.get("/api/cost/cache_economics/").json()
        self.assertTrue(cached["cached"])
        self.assertEqual(get_usage_by_key.call_count, calls_after_first)

        self.client.get("/api/cost/cache_economics/?refresh=1")
        self.assertGreater(get_usage_by_key.call_count, calls_after_first)

    def test_invalid_month_is_400(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/cost/cache_economics/?month=nope").status_code, 400)


class CostCommentaryTests(TestCase):
    """Task D: a standing, grounded "why did cost move" narrative alongside
    the monthly Insights report -- fed real monthly_stats deltas plus a
    human-maintained CostMethodologyChange changelog, never free speculation.
    Mirrors insights_views.report_insights's own grounding discipline
    (_build_prompt pins the model to known-correct numbers)."""

    def setUp(self):
        cache.clear()
        self.this_month = _now().date().replace(day=1)
        self.prev_month_start = (self.this_month - timedelta(days=2)).replace(day=1)
        # Migration 0014 seeds real CostMethodologyChange rows (dated around
        # this same investigation) into every test DB -- clear them so these
        # tests reason about exactly the fixture data they create, regardless
        # of what real entries exist now or are added later.
        from cost_management.models import CostMethodologyChange
        CostMethodologyChange.objects.all().delete()

    def tearDown(self):
        cache.clear()

    def _patch_stats(self, stats):
        p = patch("cost_management.cost_commentary._build_stats", return_value=stats)
        p.start()
        self.addCleanup(p.stop)

    def _stats(self, **overrides):
        base = {
            "month": self.this_month.strftime("%Y-%m"),
            "cost_source_error": None,
            "spend": {"total": 15.0, "prev_total": 10.0, "delta_pct": 50.0},
            "conversations": {"total": 10, "prev_total": 8, "delta_pct": 25.0},
            "per_conversation": {"cost": 1.5, "prev_cost": 1.25, "cost_delta_pct": 20.0},
            "tokens": {"cache_hit_rate": 0.8},
        }
        base.update(overrides)
        return base

    def test_changelog_window_includes_prior_and_current_month_only(self):
        from cost_management.models import CostMethodologyChange
        from cost_management.cost_commentary import _changelog_for_window

        in_prev = CostMethodologyChange.objects.create(
            date=self.prev_month_start, description="in prev month", category="measurement_fix")
        in_current = CostMethodologyChange.objects.create(
            date=self.this_month, description="in current month", category="new_feature")
        two_months_back = (self.prev_month_start - timedelta(days=1)).replace(day=1)
        CostMethodologyChange.objects.create(
            date=two_months_back, description="too old", category="incident")
        next_month = (self.this_month.replace(day=28) + timedelta(days=7)).replace(day=1)
        CostMethodologyChange.objects.create(
            date=next_month, description="too new", category="config_change")

        result = _changelog_for_window(self.this_month)

        self.assertEqual({c.pk for c in result}, {in_prev.pk, in_current.pk})

    def test_prompt_pins_the_real_numbers_and_lists_changelog_entries(self):
        from cost_management.cost_commentary import _build_cost_commentary_prompt
        from cost_management.models import CostMethodologyChange

        change = CostMethodologyChange(
            date=self.this_month, description="Fixed a cache-token undercount",
            category="measurement_fix",
        )
        prompt = _build_cost_commentary_prompt(self._stats(), [change], "2026-09")

        self.assertIn("15.0", prompt)
        self.assertIn("50.0", prompt)
        self.assertIn("Fixed a cache-token undercount", prompt)
        # C1: the prompt lists the raw category value so citations match it
        # exactly for deterministic verification.
        self.assertIn("[measurement_fix]", prompt)
        self.assertIn("spend.total", prompt)
        self.assertIn("NEVER invent", prompt)

    def test_prompt_notes_when_no_changelog_entries_exist(self):
        from cost_management.cost_commentary import _build_cost_commentary_prompt

        prompt = _build_cost_commentary_prompt(self._stats(), [], "2026-09")

        self.assertIn("none on record", prompt)

    def test_sanitize_drops_malformed_drivers_and_invalid_assessment(self):
        from cost_management.cost_commentary import _sanitize_commentary

        cleaned = _sanitize_commentary({
            "headline": 42,
            "assessment": "definitely_a_conspiracy",
            "drivers": "not a list",
        })

        self.assertEqual(cleaned["headline"], "")
        self.assertEqual(cleaned["assessment"], "insufficient_data")
        self.assertEqual(cleaned["drivers"], [])

    def test_sanitize_passes_through_well_formed_input(self):
        from cost_management.cost_commentary import _sanitize_commentary

        core = {
            "headline": "Cache accounting was fixed; spend looks flat otherwise.",
            "assessment": "measurement_artifact",
            "drivers": [{"type": "measurement_artifact", "description": "cache fix", "changelog_date": "2026-09-16"}],
        }
        self.assertEqual(_sanitize_commentary(core), core)

    @patch("cost_management.cost_commentary._generate_cost_commentary")
    def test_cost_commentary_for_returns_generated_and_sanitized_result(self, mock_gen):
        mock_gen.return_value = {
            "headline": "Real increase driven by conversation growth.",
            "assessment": "real_increase",
            "drivers": [{"type": "real_usage_change", "description": "more conversations"}],
        }
        self._patch_stats(self._stats())

        from cost_management.cost_commentary import cost_commentary_for
        result = cost_commentary_for(self.this_month)

        self.assertEqual(result["assessment"], "real_increase")
        self.assertIn("generated_at", result)
        mock_gen.assert_called_once()

    @patch("cost_management.cost_commentary._generate_cost_commentary")
    def test_cost_commentary_for_caches_within_ttl(self, mock_gen):
        mock_gen.return_value = {"headline": "x", "assessment": "no_significant_change", "drivers": []}
        self._patch_stats(self._stats())

        from cost_management.cost_commentary import cost_commentary_for
        cost_commentary_for(self.this_month)
        cost_commentary_for(self.this_month)

        mock_gen.assert_called_once()

    @patch("cost_management.cost_commentary._generate_cost_commentary", side_effect=RuntimeError("rate limited"))
    def test_cost_commentary_for_degrades_gracefully_on_generation_failure(self, mock_gen):
        self._patch_stats(self._stats())

        from cost_management.cost_commentary import cost_commentary_for
        result = cost_commentary_for(self.this_month)

        self.assertEqual(result["error"], "rate limited")

    def test_cost_commentary_for_reports_insufficient_data_on_cost_source_error(self):
        self._patch_stats(self._stats(cost_source_error="boom"))

        from cost_management.cost_commentary import cost_commentary_for
        result = cost_commentary_for(self.this_month)

        self.assertTrue(result["insufficient_data"])

    def test_cost_commentary_for_reports_insufficient_data_with_zero_conversations(self):
        self._patch_stats(self._stats(conversations={"total": 0, "prev_total": 0, "delta_pct": None}))

        from cost_management.cost_commentary import cost_commentary_for
        result = cost_commentary_for(self.this_month)

        self.assertTrue(result["insufficient_data"])


class InsightsSummaryCostCommentaryTests(TestCase):
    """cost_commentary is surfaced inside the same insights_summary endpoint
    (not a separate feature) -- see insights_views._finalize."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")

    def tearDown(self):
        cache.clear()

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    @patch("cost_management.insights_views.cost_commentary_for")
    def test_insights_summary_includes_cost_commentary(self, mock_commentary, mock_gen):
        mock_commentary.return_value = {"headline": "spend is flat", "assessment": "no_significant_change", "drivers": []}
        chat = Chat.objects.create(chat_id="c1", model="claude-haiku-4-5")
        Message.objects.create(
            chat=chat, content="hi", llm_formatted_message="{}", returned_content="hello",
            llm_formatted_returned_message="{}", tokens_in=10, tokens_out=5, model="claude-haiku-4-5",
        )
        self.client.force_login(self.user)

        data = self.client.get("/api/cost/insights_summary/").json()

        self.assertEqual(data["cost_commentary"]["headline"], "spend is flat")
        mock_commentary.assert_called_once()

    @patch("cost_management.insights_views._generate_insights", return_value=dict(CANNED_INSIGHTS))
    @patch("cost_management.insights_views.cost_commentary_for")
    def test_cost_commentary_appears_even_while_narrative_is_still_generating(self, mock_commentary, mock_gen):
        """cost_commentary must not be gated on the (slower, transcript-heavy)
        customer-insights narrative being ready -- it has its own, cheaper
        data source and should show up immediately."""
        mock_commentary.return_value = {"headline": "flat", "assessment": "no_significant_change", "drivers": []}
        self.client.force_login(self.user)
        # No conversations seeded -> current-month path falls through to the
        # "generating"/no-snapshot-yet placeholder for the transcript narrative.

        data = self.client.get("/api/cost/insights_summary/").json()

        self.assertEqual(data["cost_commentary"]["headline"], "flat")


class ModelRatesAdapterTests(TestCase):
    """Unit tests for AnthropicAdapter.get_model_rates - the $/token rate it
    derives must equal Anthropic's own billed amount divided by Anthropic's
    own reported token counts, not a guessed constant."""

    def _cost_report(self, *rows):
        return {"data": [{"results": [
            {"model": model, "cost_type": "tokens", "token_type": token_type, "amount": amount}
            for model, token_type, amount in rows
        ]}]}

    def _usage_report(self, *rows):
        return {"data": [{"results": [
            {"model": model, "uncached_input_tokens": tin, "output_tokens": tout}
            for model, tin, tout in rows
        ]}]}

    def test_derives_real_rate_from_cost_and_usage_reports(self):
        cost_resp = MagicMock(status_code=200, json=lambda: self._cost_report(
            ("claude-haiku-4-5", "uncached_input_tokens", "100.0"),   # $1.00
            ("claude-haiku-4-5", "output_tokens", "250.0"),           # $2.50
        ))
        usage_resp = MagicMock(status_code=200, json=lambda: self._usage_report(
            ("claude-haiku-4-5", 1_000_000, 500_000),
        ))
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[cost_resp, usage_resp],
        ):
            result = AnthropicAdapter().get_model_rates(year=2026, month=8)

        self.assertEqual(result["rates"]["claude-haiku-4-5"]["input"], 1.0 / 1_000_000)
        self.assertEqual(result["rates"]["claude-haiku-4-5"]["output"], 2.5 / 500_000)

    def test_model_with_no_usage_data_is_omitted(self):
        cost_resp = MagicMock(status_code=200, json=lambda: self._cost_report(
            ("claude-opus-5", "uncached_input_tokens", "50.0"),
        ))
        usage_resp = MagicMock(status_code=200, json=lambda: self._usage_report())
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[cost_resp, usage_resp],
        ):
            from .llm_provider_adapter_implementations import AnthropicAdapter
            result = AnthropicAdapter().get_model_rates(year=2026, month=8)

        self.assertEqual(result["rates"], {})

    def test_cost_report_failure_returns_error(self):
        cost_resp = MagicMock(status_code=500, text="boom")
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[cost_resp],
        ):
            from .llm_provider_adapter_implementations import AnthropicAdapter
            result = AnthropicAdapter().get_model_rates(year=2026, month=8)

        self.assertEqual(result["error"], "boom")


class WholeOrgRateDerivationTests(TestCase):
    """get_model_rates computes a $/token UNIT rate, not an absolute total --
    Anthropic prices uniformly per model/token-type across the whole org, so
    the rate is accurate regardless of which keys generated the underlying
    cost_report/usage_report rows. Both sides of the ratio must cover the
    SAME population for the arithmetic to be valid at all, and since
    cost_report can never be scoped by api_key_id (only description/
    workspace_id -- and workspace_id comes back null on real cache-cost line
    items even for workspace-scoped keys, confirmed live 2026-09-16), the
    only valid choice for BOTH sides is whole-org, unfiltered.

    A prior attempt to scope just the usage_report side to a handful of
    tracked key ids was tried and retracted the same day: right after
    rotating to new per-surface keys, most of the month's spend still sat
    under the old, now-untracked shared key, so a tiny token denominator
    (hours of the new keys' traffic) divided into a full month of org-wide
    cost inflated one real chat's estimated cost by ~1500x ($35 for ~53k
    Haiku tokens that should cost about $0.02).

    Contrast with AppApiKeyScopingTests below: get_cost/get_tokens report
    ABSOLUTE totals, which DO need real scoping -- unlike a unit rate, an
    absolute total is directly inflated by any unrelated project sharing the
    same Anthropic org (confirmed live 2026-09-16: this org also holds
    Claude Code, BudgetETL, Photo Highlights, and Roblox Studio workspaces,
    unknown to this app until that point)."""

    def test_get_model_rates_ignores_app_api_key_ids_and_the_legacy_workspace_env_var(self):
        cost_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "cost_type": "tokens", "token_type": "uncached_input_tokens", "amount": "100.0"},
        ]}]})
        usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_target", "uncached_input_tokens": 1_000_000, "output_tokens": 0},
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_other", "uncached_input_tokens": 9_000_000, "output_tokens": 0},
        ]}]})
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch.dict('os.environ', {
            'ANTHROPIC_APP_API_KEY_IDS': 'apikey_target',
            'ANTHROPIC_WORKSPACE_ID': 'wrkspc_target',
        }), patch("cost_management.llm_provider_adapter_implementations.requests.get",
                  side_effect=[cost_resp, usage_resp]) as mock_get:
            result = AnthropicAdapter().get_model_rates(year=2026, month=8)

        # $1.00 whole-org / 10,000,000 whole-org tokens -- both keys counted.
        self.assertEqual(result["rates"]["claude-haiku-4-5"]["input"], 1.0 / 10_000_000)
        cost_call_params = mock_get.call_args_list[0].kwargs["params"]
        self.assertEqual(cost_call_params["group_by[]"], "description")
        usage_call_params = mock_get.call_args_list[1].kwargs["params"]
        self.assertNotIn("workspace_ids[]", usage_call_params)

    def test_get_model_rates_stays_accurate_across_a_key_rotation(self):
        """The incident regression test: cost_report is whole-month/whole-org
        regardless of when keys rotated, so usage_report must stay whole-org
        too -- otherwise a fresh key with only hours of traffic gets divided
        into a full month of org spend and the rate blows up."""
        cost_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "cost_type": "tokens", "token_type": "uncached_input_tokens", "amount": "100000.0"},
        ]}]})
        usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            # 99.9% of the month's traffic under a key that's since rotated out.
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_old_rotated_out", "uncached_input_tokens": 999_000_000, "output_tokens": 0},
            # Brand-new key, only hours of traffic so far.
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_new_tracked", "uncached_input_tokens": 1_000_000, "output_tokens": 0},
        ]}]})
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch.dict('os.environ', {'ANTHROPIC_APP_API_KEY_IDS': 'apikey_new_tracked'}), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get",
                   side_effect=[cost_resp, usage_resp]):
            result = AnthropicAdapter().get_model_rates(year=2026, month=9)

        # $1000 / 1,000,000,000 whole-org tokens = $1/1M, not $1000/1M.
        self.assertEqual(result["rates"]["claude-haiku-4-5"]["input"], 1000.0 / 1_000_000_000)


class AppApiKeyScopingTests(TestCase):
    """get_cost/get_tokens report ABSOLUTE totals for this app specifically,
    scoped to ANTHROPIC_APP_API_KEY_IDS (comma-separated Anthropic
    api_key_id values -- this app's own production keys, past and present,
    e.g. the old pre-rotation shared key plus the current per-surface
    keys). Unlike get_model_rates' $/token unit rate (see
    WholeOrgRateDerivationTests above), an absolute total is directly
    inflated by any other project sharing the same Anthropic org --
    confirmed live 2026-09-16 when a "whole-org" total included ~$37 from
    four unrelated workspaces (Claude Code, BudgetETL, Photo Highlights,
    Roblox Studio) this app had no way to know about.

    get_tokens filters usage_report rows by api_key_id directly (a real,
    reliable, per-request field -- unlike workspace_id, which comes back
    null on real cache-cost rows). get_cost cannot do the equivalent against
    cost_report (no per-key filter exists there at all), so it instead
    multiplies get_model_rates' whole-org $/token rates by this app's own
    api_key_id-scoped token counts -- the same estimation technique
    stats_views.usage_by_key already uses per individual key, applied here
    to the whole app's total."""

    def test_get_tokens_filters_to_app_api_key_ids_when_set(self):
        resp = MagicMock(status_code=200, json=lambda: {"data": [{"starting_at": "2026-08-01T00:00:00Z", "results": [
            {"api_key_id": "apikey_target", "uncached_input_tokens": 100, "output_tokens": 10},
            {"api_key_id": "apikey_other", "uncached_input_tokens": 900, "output_tokens": 90},
        ]}]})
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch.dict('os.environ', {'ANTHROPIC_APP_API_KEY_IDS': 'apikey_target'}), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get",
                   return_value=resp):
            result = AnthropicAdapter().get_tokens(year=2026, month=8)

        self.assertEqual(result["tokens"][0]["input_tokens"], 100)
        self.assertEqual(result["tokens"][0]["output_tokens"], 10)

    def test_get_tokens_includes_every_key_by_default(self):
        resp = MagicMock(status_code=200, json=lambda: {"data": [{"starting_at": "2026-08-01T00:00:00Z", "results": [
            {"api_key_id": "apikey_a", "uncached_input_tokens": 100, "output_tokens": 10},
            {"api_key_id": "apikey_b", "uncached_input_tokens": 900, "output_tokens": 90},
        ]}]})
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch.dict('os.environ', {}, clear=False):
            os.environ.pop('ANTHROPIC_APP_API_KEY_IDS', None)
            with patch("cost_management.llm_provider_adapter_implementations.requests.get",
                       return_value=resp):
                result = AnthropicAdapter().get_tokens(year=2026, month=8)

        self.assertEqual(result["tokens"][0]["input_tokens"], 1000)

    def test_get_tokens_ignores_the_legacy_workspace_env_var(self):
        resp = MagicMock(status_code=200, json=lambda: {"data": []})
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch.dict('os.environ', {'ANTHROPIC_WORKSPACE_ID': 'wrkspc_target'}), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get",
                   return_value=resp) as mock_get:
            AnthropicAdapter().get_tokens(year=2026, month=8)

        params = mock_get.call_args.kwargs["params"]
        self.assertNotIn("workspace_ids[]", params)

    def test_get_cost_estimates_from_rates_times_app_key_tokens(self):
        cost_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "cost_type": "tokens", "token_type": "uncached_input_tokens", "amount": "100.0"},
        ]}]})
        rate_usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "uncached_input_tokens": 1_000_000, "output_tokens": 0},
        ]}]})
        own_usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"starting_at": "2026-08-01T00:00:00Z", "results": [
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_target", "uncached_input_tokens": 500_000, "output_tokens": 0},
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_other", "uncached_input_tokens": 9_000_000, "output_tokens": 0},
        ]}]})
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch.dict('os.environ', {'ANTHROPIC_APP_API_KEY_IDS': 'apikey_target'}), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get",
                   side_effect=[cost_resp, rate_usage_resp, own_usage_resp]):
            result = AnthropicAdapter().get_cost(year=2026, month=8)

        # rate = $1.00 / 1,000,000 = $0.000001/token; our tokens = 500,000
        self.assertEqual(result["costs"][0]["total_cost"], 0.5)
        self.assertEqual(result["monthly_average_cost"], 0.5)

    def test_get_cost_excludes_unrelated_workspaces_traffic_when_scoped(self):
        """The live incident regression: this org also holds unrelated
        Claude Code / BudgetETL / Photo Highlights / Roblox Studio traffic
        -- a huge unrelated key's tokens must never leak into this app's
        cost total just because they share the same Anthropic org."""
        cost_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "cost_type": "tokens", "token_type": "uncached_input_tokens", "amount": "100.0"},
        ]}]})
        rate_usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "uncached_input_tokens": 1_000_000, "output_tokens": 0},
        ]}]})
        own_usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"starting_at": "2026-08-01T00:00:00Z", "results": [
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_old_chatbot", "uncached_input_tokens": 20_000_000, "output_tokens": 0},
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_new_chatbot", "uncached_input_tokens": 1_000_000, "output_tokens": 0},
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_roblox_unrelated", "uncached_input_tokens": 50_000_000, "output_tokens": 0},
        ]}]})
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch.dict('os.environ', {'ANTHROPIC_APP_API_KEY_IDS': 'apikey_old_chatbot,apikey_new_chatbot'}), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get",
                   side_effect=[cost_resp, rate_usage_resp, own_usage_resp]):
            result = AnthropicAdapter().get_cost(year=2026, month=8)

        # (20M + 1M) tokens * $0.000001/token = $21.00 -- Roblox's 50M excluded.
        self.assertEqual(result["costs"][0]["total_cost"], 21.0)

    def test_get_cost_propagates_rate_derivation_errors(self):
        cost_resp = MagicMock(status_code=500, text="boom")
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[cost_resp],
        ):
            result = AnthropicAdapter().get_cost(year=2026, month=8)

        self.assertEqual(result["error"], "boom")


class GetCostRatesRespReuseTests(TestCase):
    """get_cost's optional rates_resp param lets a caller that already
    fetched get_model_rates for this exact month pass it straight in --
    added because stats_views._build_stats/cost_reconciliation both need
    this month's whole-org rate for their own proration math regardless of
    whether get_cost succeeds, and were each triggering a second, identical
    cost_report+usage_report pair by letting get_cost derive its own on top
    of that. Must produce the exact same result as the two-call path, using
    only the one HTTP call get_cost's own estimate legitimately needs."""

    def test_uses_passed_rates_instead_of_deriving_its_own(self):
        own_usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"starting_at": "2026-08-01T00:00:00Z", "results": [
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_target", "uncached_input_tokens": 500_000, "output_tokens": 0},
        ]}]})
        rates_resp = {"rates": {"claude-haiku-4-5": {"input": 0.000001}}}
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch.dict('os.environ', {'ANTHROPIC_APP_API_KEY_IDS': 'apikey_target'}), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get",
                   side_effect=[own_usage_resp]) as mock_get:
            result = AnthropicAdapter().get_cost(year=2026, month=8, rates_resp=rates_resp)

        # Exactly one HTTP call (its own usage_report) -- no cost_report/
        # usage_report pair for a rate derivation it was handed already.
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(result["costs"][0]["total_cost"], 0.5)

    def test_propagates_an_error_already_present_on_the_passed_rates_resp(self):
        """A pre-fetched rates_resp that failed must still surface as a
        get_cost error, not silently price everything at $0 -- same
        contract as when get_cost derives the rate itself."""
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch("cost_management.llm_provider_adapter_implementations.requests.get") as mock_get:
            result = AnthropicAdapter().get_cost(year=2026, month=8, rates_resp={"error": "boom"})

        mock_get.assert_not_called()
        self.assertEqual(result["error"], "boom")


class ChatApiKeyScopingTests(TestCase):
    """The "Cost / conversation" KPI (stats_views.cost_pc) divides an
    Anthropic-spend numerator by a chat-conversation-only denominator
    (Chat rows only ever come from the chatbot's own log_message calls --
    the AI Search Curator/narrative/report surfaces never write one). Before
    this, the numerator was scoped to ANTHROPIC_APP_API_KEY_IDS -- ALL of
    this app's keys, chat AND search AND report (ENG-147's three-way key
    split) -- so any growth in curator/narrative/report usage inflated
    cost/conversation with zero matching increase in the denominator. This
    adds an optional, narrower key_ids override so the stats endpoint can
    scope to chat-only keys specifically, while every other/existing caller
    (which doesn't pass key_ids) keeps the prior app-wide behavior."""

    def test_get_cost_accepts_a_key_ids_override_narrower_than_app_api_key_ids(self):
        cost_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "cost_type": "tokens", "token_type": "uncached_input_tokens", "amount": "100.0"},
        ]}]})
        rate_usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "uncached_input_tokens": 1_000_000, "output_tokens": 0},
        ]}]})
        own_usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"starting_at": "2026-08-01T00:00:00Z", "results": [
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_chat", "uncached_input_tokens": 500_000, "output_tokens": 0},
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_search", "uncached_input_tokens": 9_000_000, "output_tokens": 0},
        ]}]})
        from .llm_provider_adapter_implementations import AnthropicAdapter
        # ANTHROPIC_APP_API_KEY_IDS covers both keys, but the explicit
        # key_ids override passed to get_cost should win over it.
        with patch.dict('os.environ', {'ANTHROPIC_APP_API_KEY_IDS': 'apikey_chat,apikey_search'}), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get",
                   side_effect=[cost_resp, rate_usage_resp, own_usage_resp]):
            result = AnthropicAdapter().get_cost(year=2026, month=8, key_ids=["apikey_chat"])

        # $0.000001/token * 500,000 chat-only tokens = $0.50 -- search's 9M excluded.
        self.assertEqual(result["costs"][0]["total_cost"], 0.5)

    def test_get_cost_falls_back_to_app_api_key_ids_when_key_ids_not_passed(self):
        """Backward compatibility: every existing caller that doesn't pass
        key_ids must see exactly the prior app-wide-scoped behavior."""
        cost_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "cost_type": "tokens", "token_type": "uncached_input_tokens", "amount": "100.0"},
        ]}]})
        rate_usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "uncached_input_tokens": 1_000_000, "output_tokens": 0},
        ]}]})
        own_usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"starting_at": "2026-08-01T00:00:00Z", "results": [
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_chat", "uncached_input_tokens": 500_000, "output_tokens": 0},
            {"model": "claude-haiku-4-5", "api_key_id": "apikey_search", "uncached_input_tokens": 9_000_000, "output_tokens": 0},
        ]}]})
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch.dict('os.environ', {'ANTHROPIC_APP_API_KEY_IDS': 'apikey_chat,apikey_search'}), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get",
                   side_effect=[cost_resp, rate_usage_resp, own_usage_resp]):
            result = AnthropicAdapter().get_cost(year=2026, month=8)

        # Both keys included -- unchanged prior behavior.
        self.assertEqual(result["costs"][0]["total_cost"], 9.5)

    def test_get_tokens_accepts_a_key_ids_override(self):
        resp = MagicMock(status_code=200, json=lambda: {"data": [{"starting_at": "2026-08-01T00:00:00Z", "results": [
            {"api_key_id": "apikey_chat", "uncached_input_tokens": 100, "output_tokens": 10},
            {"api_key_id": "apikey_search", "uncached_input_tokens": 900, "output_tokens": 90},
        ]}]})
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch.dict('os.environ', {'ANTHROPIC_APP_API_KEY_IDS': 'apikey_chat,apikey_search'}), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get",
                   return_value=resp):
            result = AnthropicAdapter().get_tokens(year=2026, month=8, key_ids=["apikey_chat"])

        self.assertEqual(result["tokens"][0]["input_tokens"], 100)

    def test_chat_api_key_ids_falls_back_to_app_api_key_ids_when_unset(self):
        from .llm_provider_adapter_implementations import chat_api_key_ids
        with patch.dict('os.environ', {'ANTHROPIC_APP_API_KEY_IDS': 'apikey_chat,apikey_search'}, clear=False):
            os.environ.pop('ANTHROPIC_CHAT_API_KEY_IDS', None)
            self.assertEqual(chat_api_key_ids(), ['apikey_chat', 'apikey_search'])

    def test_chat_api_key_ids_uses_its_own_env_var_when_set(self):
        from .llm_provider_adapter_implementations import chat_api_key_ids
        with patch.dict('os.environ', {
            'ANTHROPIC_APP_API_KEY_IDS': 'apikey_chat,apikey_search,apikey_report',
            'ANTHROPIC_CHAT_API_KEY_IDS': 'apikey_chat',
        }):
            self.assertEqual(chat_api_key_ids(), ['apikey_chat'])


class CacheTokenAdapterTests(TestCase):
    """ENG-148: get_tokens/get_model_rates previously only ever read
    uncached_input_tokens, silently excluding cache_creation/cache_read
    tokens from both the dashboard's "Tokens" KPI and the per-model rates
    used to estimate per-chat cost -- even though those are real, billed
    tokens Anthropic reports on every response. Shapes here are the real
    usage_report/messages and cost_report response shapes, confirmed
    directly against the live API 2026-09-16 (cache_creation is a nested
    object with per-TTL sub-fields; cache_read_input_tokens is flat;
    cost_report's matching token_type strings are
    "cache_creation.ephemeral_5m_input_tokens" /
    "cache_creation.ephemeral_1h_input_tokens" / "cache_read_input_tokens")."""

    def _usage_report(self, *rows):
        """rows: (uncached_in, cache_5m, cache_1h, cache_read, output)"""
        return {"data": [{"starting_at": "2026-09-01T00:00:00Z", "results": [
            {
                "model": "claude-haiku-4-5",
                "uncached_input_tokens": u,
                "cache_creation": {"ephemeral_5m_input_tokens": c5, "ephemeral_1h_input_tokens": c1},
                "cache_read_input_tokens": r,
                "output_tokens": o,
            }
            for u, c5, c1, r, o in rows
        ]}]}

    def test_get_tokens_input_total_includes_cache_creation_and_read(self):
        # true total input = 1000 (uncached) + 200 (5m write) + 100 (1h write) + 3000 (read) = 4300
        usage_resp = MagicMock(status_code=200, json=lambda: self._usage_report(
            (1000, 200, 100, 3000, 50),
        ))
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=usage_resp):
            result = AnthropicAdapter().get_tokens(year=2026, month=9)

        self.assertEqual(result["tokens"][0]["input_tokens"], 4300)
        self.assertEqual(result["tokens"][0]["output_tokens"], 50)

    def test_get_tokens_reports_cache_breakdown_and_hit_rate(self):
        usage_resp = MagicMock(status_code=200, json=lambda: self._usage_report(
            (1000, 200, 100, 3000, 50),
        ))
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=usage_resp):
            result = AnthropicAdapter().get_tokens(year=2026, month=9)

        self.assertEqual(result["cache"]["creation_tokens"], 300)
        self.assertEqual(result["cache"]["read_tokens"], 3000)
        # hit rate = cache_read / true_total_input = 3000 / 4300
        self.assertAlmostEqual(result["cache"]["hit_rate"], 3000 / 4300, places=6)

    def test_get_tokens_cache_hit_rate_is_none_with_zero_input(self):
        usage_resp = MagicMock(status_code=200, json=lambda: {"data": []})
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=usage_resp):
            result = AnthropicAdapter().get_tokens(year=2026, month=9)

        self.assertIsNone(result["cache"]["hit_rate"])
        self.assertEqual(result["cache"]["creation_tokens"], 0)
        self.assertEqual(result["cache"]["read_tokens"], 0)

    def test_get_model_rates_derives_cache_creation_and_read_rates(self):
        cost_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "cost_type": "tokens",
             "token_type": "cache_creation.ephemeral_5m_input_tokens", "amount": "125.0"},  # $1.25
            {"model": "claude-haiku-4-5", "cost_type": "tokens",
             "token_type": "cache_read_input_tokens", "amount": "10.0"},  # $0.10
        ]}]})
        usage_resp = MagicMock(status_code=200, json=lambda: self._usage_report(
            (0, 1_000_000, 0, 1_000_000, 0),
        ))
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[cost_resp, usage_resp],
        ):
            result = AnthropicAdapter().get_model_rates(year=2026, month=9)

        rates = result["rates"]["claude-haiku-4-5"]
        self.assertAlmostEqual(rates["cache_creation"], 1.25 / 1_000_000, places=10)
        self.assertAlmostEqual(rates["cache_read"], 0.10 / 1_000_000, places=10)

    def test_get_model_rates_sums_both_cache_creation_ttls_into_one_rate(self):
        cost_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"model": "claude-haiku-4-5", "cost_type": "tokens",
             "token_type": "cache_creation.ephemeral_5m_input_tokens", "amount": "100.0"},
            {"model": "claude-haiku-4-5", "cost_type": "tokens",
             "token_type": "cache_creation.ephemeral_1h_input_tokens", "amount": "200.0"},
        ]}]})
        usage_resp = MagicMock(status_code=200, json=lambda: self._usage_report(
            (0, 500_000, 500_000, 0, 0),
        ))
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[cost_resp, usage_resp],
        ):
            result = AnthropicAdapter().get_model_rates(year=2026, month=9)

        # (100+200) cents over (500k+500k) tokens = $3.00 / 1M tokens
        self.assertEqual(result["rates"]["claude-haiku-4-5"]["cache_creation"], 3.0 / 1_000_000)


class UsageByKeyAdapterTests(TestCase):
    """Unit tests for AnthropicAdapter.get_usage_by_key. Anthropic's
    cost_report can't group by individual API key at all (confirmed against
    the real API 2026-09-16 -- only description/workspace_id are valid
    group_by values), so per-key attribution can only ever come from
    usage_report/messages (token counts), grouped by api_key_id + model."""

    def _usage_report(self, *rows):
        """rows: (api_key_id, model, input_tokens, output_tokens)"""
        return {"data": [{"results": [
            {"api_key_id": kid, "model": model, "uncached_input_tokens": tin, "output_tokens": tout}
            for kid, model, tin, tout in rows
        ]}]}

    def _keys_list(self, *pairs):
        """pairs: (id, name)"""
        return {"data": [{"id": kid, "name": name} for kid, name in pairs]}

    def test_aggregates_tokens_per_key_across_models(self):
        usage_resp = MagicMock(status_code=200, json=lambda: self._usage_report(
            ("apikey_chat", "claude-haiku-4-5", 1000, 200),
            ("apikey_chat", "claude-sonnet-5", 500, 100),
            ("apikey_search", "claude-haiku-4-5", 300, 50),
        ))
        keys_resp = MagicMock(status_code=200, json=lambda: self._keys_list(
            ("apikey_chat", "prod-shopify-chatbot"),
            ("apikey_search", "prod-shopify-search"),
        ))
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[usage_resp, keys_resp],
        ):
            result = AnthropicAdapter().get_usage_by_key(year=2026, month=9)

        by_id = {k["api_key_id"]: k for k in result["keys"]}
        self.assertEqual(by_id["apikey_chat"]["name"], "prod-shopify-chatbot")
        self.assertEqual(by_id["apikey_chat"]["input_tokens"], 1500)
        self.assertEqual(by_id["apikey_chat"]["output_tokens"], 300)
        self.assertEqual(by_id["apikey_chat"]["by_model"]["claude-sonnet-5"]["uncached_input_tokens"], 500)
        self.assertEqual(by_id["apikey_search"]["name"], "prod-shopify-search")
        self.assertEqual(by_id["apikey_search"]["input_tokens"], 300)

    def test_input_tokens_include_cache_creation_and_read_eng_148(self):
        """Same gap ENG-148 fixed for get_tokens: a key's input_tokens must
        be the TRUE total (uncached + both cache directions), not just the
        uncached slice -- otherwise per-key rows can never sum to the
        dashboard's own "Anthropic spend"/"Tokens" KPIs, which do include
        cache tokens."""
        usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [{
            "api_key_id": "apikey_chat",
            "model": "claude-haiku-4-5",
            "uncached_input_tokens": 1000,
            "cache_creation": {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 200},
            "cache_read_input_tokens": 4000,
            "output_tokens": 50,
        }]}]})
        keys_resp = MagicMock(status_code=200, json=lambda: self._keys_list())
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[usage_resp, keys_resp],
        ):
            result = AnthropicAdapter().get_usage_by_key(year=2026, month=9)

        key = result["keys"][0]
        # 1000 uncached + 300 + 200 cache creation + 4000 cache read = 5500
        self.assertEqual(key["input_tokens"], 5500)
        by_model = key["by_model"]["claude-haiku-4-5"]
        self.assertEqual(by_model["uncached_input_tokens"], 1000)
        self.assertEqual(by_model["cache_creation_tokens"], 500)
        self.assertEqual(by_model["cache_read_tokens"], 4000)

    def test_unknown_key_id_falls_back_to_the_raw_id_as_name(self):
        usage_resp = MagicMock(status_code=200, json=lambda: self._usage_report(
            ("apikey_mystery", "claude-haiku-4-5", 10, 5),
        ))
        keys_resp = MagicMock(status_code=200, json=lambda: self._keys_list())
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[usage_resp, keys_resp],
        ):
            result = AnthropicAdapter().get_usage_by_key(year=2026, month=9)

        self.assertEqual(result["keys"][0]["name"], "apikey_mystery")

    def test_results_with_no_api_key_id_are_skipped(self):
        usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{"results": [
            {"api_key_id": None, "model": "claude-haiku-4-5", "uncached_input_tokens": 999, "output_tokens": 999},
        ]}]})
        keys_resp = MagicMock(status_code=200, json=lambda: self._keys_list())
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[usage_resp, keys_resp],
        ):
            result = AnthropicAdapter().get_usage_by_key(year=2026, month=9)

        self.assertEqual(result["keys"], [])

    def test_sorted_by_total_tokens_descending(self):
        usage_resp = MagicMock(status_code=200, json=lambda: self._usage_report(
            ("apikey_small", "claude-haiku-4-5", 10, 10),
            ("apikey_big", "claude-haiku-4-5", 1000, 1000),
        ))
        keys_resp = MagicMock(status_code=200, json=lambda: self._keys_list())
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[usage_resp, keys_resp],
        ):
            result = AnthropicAdapter().get_usage_by_key(year=2026, month=9)

        self.assertEqual([k["api_key_id"] for k in result["keys"]], ["apikey_big", "apikey_small"])

    def test_usage_report_failure_returns_error(self):
        usage_resp = MagicMock(status_code=500, text="boom")
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[usage_resp],
        ):
            result = AnthropicAdapter().get_usage_by_key(year=2026, month=9)

        self.assertEqual(result["error"], "boom")

    def test_key_names_lookup_failure_falls_back_to_raw_ids(self):
        """A broken /api_keys call shouldn't sink usage data that already
        succeeded -- degrade to raw ids as names, don't error the whole call."""
        usage_resp = MagicMock(status_code=200, json=lambda: self._usage_report(
            ("apikey_chat", "claude-haiku-4-5", 10, 5),
        ))
        keys_resp = MagicMock(status_code=500, text="boom")
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch(
            "cost_management.llm_provider_adapter_implementations.requests.get",
            side_effect=[usage_resp, keys_resp],
        ):
            result = AnthropicAdapter().get_usage_by_key(year=2026, month=9)

        self.assertEqual(result["keys"][0]["name"], "apikey_chat")

    def test_is_never_scoped_by_the_legacy_workspace_env_var(self):
        """This view's whole purpose is to show every key with usage
        (including one nobody expected), so it must never filter itself
        down -- unlike the other adapter methods, it never had a
        tracked-key-id scoping option to begin with."""
        usage_resp = MagicMock(status_code=200, json=lambda: self._usage_report())
        keys_resp = MagicMock(status_code=200, json=lambda: self._keys_list())
        from .llm_provider_adapter_implementations import AnthropicAdapter
        with patch.dict('os.environ', {'ANTHROPIC_WORKSPACE_ID': 'wrkspc_target'}), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get",
                   side_effect=[usage_resp, keys_resp]) as mock_get:
            AnthropicAdapter().get_usage_by_key(year=2026, month=9)

        usage_call_params = mock_get.call_args_list[0].kwargs["params"]
        self.assertNotIn("workspace_ids[]", usage_call_params)


class UsageByKeyEndpointTests(TestCase):
    """Combines get_usage_by_key (tokens) with get_model_rates (effective
    $/token) to produce an estimated cost per key -- necessarily an estimate,
    since Anthropic's cost_report has no per-key breakdown to derive a real
    billed figure from (see UsageByKeyAdapterTests' docstring)."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")

    def tearDown(self):
        cache.clear()

    def _patch_adapter(self, usage_return=None, rates_return=None):
        get_usage_by_key = MagicMock(return_value=usage_return or {"keys": [], "workspace_id": None})
        get_model_rates = MagicMock(return_value=rates_return or {"rates": {}})
        p = patch.multiple(
            "cost_management.views.llmprovider",
            get_usage_by_key=get_usage_by_key,
            get_model_rates=get_model_rates,
        )
        p.start()
        self.addCleanup(p.stop)
        return get_usage_by_key, get_model_rates

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/cost/get_usage_by_key/").status_code, 401)

    def test_combines_tokens_and_rates_into_estimated_cost(self):
        self._patch_adapter(
            usage_return={"keys": [{
                "api_key_id": "apikey_chat", "name": "prod-shopify-chatbot",
                "input_tokens": 1_000_000, "output_tokens": 500_000,
                "by_model": {"claude-haiku-4-5": {
                    "uncached_input_tokens": 1_000_000, "output_tokens": 500_000,
                    "cache_creation_tokens": 0, "cache_read_tokens": 0,
                }},
            }], "workspace_id": "wrkspc_target"},
            rates_return={"rates": {"claude-haiku-4-5": {"input": 0.000001, "output": 0.000005}}},
        )
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/get_usage_by_key/").json()

        self.assertEqual(d["keys"][0]["estimated_cost"], 1.0 + 2.5)
        self.assertTrue(d["estimated"])

    def test_estimated_cost_includes_cache_creation_and_read_at_their_own_rates(self):
        """Per-key cost must price cache tokens at their own rate (not the
        plain input rate, and not drop them) -- otherwise this panel's rows
        can never sum to the "Anthropic spend" KPI, which does include
        cache costs (see get_cost)."""
        self._patch_adapter(
            usage_return={"keys": [{
                "api_key_id": "apikey_chat", "name": "prod-shopify-chatbot",
                "input_tokens": 1_000_100, "output_tokens": 0,
                "by_model": {"claude-haiku-4-5": {
                    "uncached_input_tokens": 100, "output_tokens": 0,
                    "cache_creation_tokens": 1_000, "cache_read_tokens": 999_000,
                }},
            }]},
            rates_return={"rates": {"claude-haiku-4-5": {
                "input": 0.000001, "cache_creation": 0.00000125, "cache_read": 0.0000001,
            }}},
        )
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/get_usage_by_key/").json()

        # 100*0.000001 + 1000*0.00000125 + 999000*0.0000001 = 0.0001 + 0.00125 + 0.0999
        self.assertEqual(d["keys"][0]["estimated_cost"], round(0.0001 + 0.00125 + 0.0999, 2))

    def test_missing_rate_for_a_model_contributes_zero_not_an_error(self):
        self._patch_adapter(
            usage_return={"keys": [{
                "api_key_id": "apikey_x", "name": "x",
                "input_tokens": 100, "output_tokens": 100,
                "by_model": {"some-unpriced-model": {
                    "uncached_input_tokens": 100, "output_tokens": 100,
                    "cache_creation_tokens": 0, "cache_read_tokens": 0,
                }},
            }], "workspace_id": None},
            rates_return={"rates": {}},
        )
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/get_usage_by_key/").json()

        self.assertEqual(d["keys"][0]["estimated_cost"], 0.0)

    def test_filters_to_app_api_key_ids_when_set(self):
        """The panel must read as 'usage by our keys', not every key in the
        org -- unlike get_usage_by_key itself, which stays deliberately
        unscoped (see UsageByKeyAdapterTests)."""
        self._patch_adapter(
            usage_return={"keys": [
                {"api_key_id": "apikey_ours", "name": "prod-shopify-chatbot",
                 "input_tokens": 100, "output_tokens": 0,
                 "by_model": {"m": {"uncached_input_tokens": 100, "output_tokens": 0,
                                     "cache_creation_tokens": 0, "cache_read_tokens": 0}}},
                {"api_key_id": "apikey_unrelated", "name": "budget-etl-key",
                 "input_tokens": 5_000_000, "output_tokens": 0,
                 "by_model": {"m": {"uncached_input_tokens": 5_000_000, "output_tokens": 0,
                                     "cache_creation_tokens": 0, "cache_read_tokens": 0}}},
            ]},
            rates_return={"rates": {"m": {"input": 0.000001}}},
        )
        self.client.force_login(self.user)

        with patch.dict('os.environ', {'ANTHROPIC_APP_API_KEY_IDS': 'apikey_ours'}):
            d = self.client.get("/api/cost/get_usage_by_key/").json()

        self.assertEqual([k["api_key_id"] for k in d["keys"]], ["apikey_ours"])

    def test_shows_every_key_when_app_api_key_ids_unset(self):
        self._patch_adapter(
            usage_return={"keys": [
                {"api_key_id": "apikey_a", "name": "a", "input_tokens": 10, "output_tokens": 0,
                 "by_model": {"m": {"uncached_input_tokens": 10, "output_tokens": 0,
                                     "cache_creation_tokens": 0, "cache_read_tokens": 0}}},
                {"api_key_id": "apikey_b", "name": "b", "input_tokens": 5, "output_tokens": 0,
                 "by_model": {"m": {"uncached_input_tokens": 5, "output_tokens": 0,
                                     "cache_creation_tokens": 0, "cache_read_tokens": 0}}},
            ]},
            rates_return={"rates": {"m": {"input": 0.0}}},
        )
        self.client.force_login(self.user)

        with patch.dict('os.environ', {}, clear=False):
            os.environ.pop('ANTHROPIC_APP_API_KEY_IDS', None)
            d = self.client.get("/api/cost/get_usage_by_key/").json()

        self.assertEqual({k["api_key_id"] for k in d["keys"]}, {"apikey_a", "apikey_b"})

    def test_usage_source_error_returns_empty_keys_with_error(self):
        self._patch_adapter(usage_return={"error": "boom"})
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/get_usage_by_key/").json()

        self.assertEqual(d["keys"], [])
        self.assertEqual(d["error"], "boom")

    def test_caches_and_refresh_bypasses(self):
        get_usage_by_key, _ = self._patch_adapter()
        self.client.force_login(self.user)

        self.client.get("/api/cost/get_usage_by_key/")
        calls_after_first = get_usage_by_key.call_count
        cached = self.client.get("/api/cost/get_usage_by_key/").json()
        self.assertTrue(cached["cached"])
        self.assertEqual(get_usage_by_key.call_count, calls_after_first)

        self.client.get("/api/cost/get_usage_by_key/?refresh=1")
        self.assertGreater(get_usage_by_key.call_count, calls_after_first)

    def test_invalid_month_is_400(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/cost/get_usage_by_key/?month=nope").status_code, 400)

    def test_keys_sorted_by_estimated_cost_descending(self):
        self._patch_adapter(
            usage_return={"keys": [
                {"api_key_id": "small", "name": "small", "input_tokens": 10, "output_tokens": 0,
                 "by_model": {"m": {"uncached_input_tokens": 10, "output_tokens": 0,
                                     "cache_creation_tokens": 0, "cache_read_tokens": 0}}},
                {"api_key_id": "big", "name": "big", "input_tokens": 1000, "output_tokens": 0,
                 "by_model": {"m": {"uncached_input_tokens": 1000, "output_tokens": 0,
                                     "cache_creation_tokens": 0, "cache_read_tokens": 0}}},
            ], "workspace_id": None},
            rates_return={"rates": {"m": {"input": 0.01, "output": 0.0}}},
        )
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/get_usage_by_key/").json()

        self.assertEqual([k["api_key_id"] for k in d["keys"]], ["big", "small"])


class ModelRatesEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")
        self.this_month = _now().replace(day=1)

    def tearDown(self):
        cache.clear()

    def _patch_adapter(self, return_value=None):
        get_model_rates = MagicMock(
            return_value=return_value or {"rates": {"claude-haiku-4-5": {"input": 1e-6, "output": 5e-6}}}
        )
        p = patch.multiple("cost_management.views.llmprovider", get_model_rates=get_model_rates)
        p.start()
        self.addCleanup(p.stop)
        return get_model_rates

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/cost/get_model_rates/").status_code, 401)

    def test_returns_rates_from_adapter(self):
        self._patch_adapter()
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/get_model_rates/").json()

        self.assertEqual(d["rates"]["claude-haiku-4-5"]["input"], 1e-6)

    def test_caches_and_refresh_bypasses(self):
        get_model_rates = self._patch_adapter()
        self.client.force_login(self.user)

        self.client.get("/api/cost/get_model_rates/")
        calls_after_first = get_model_rates.call_count
        cached = self.client.get("/api/cost/get_model_rates/").json()
        self.assertTrue(cached["cached"])
        self.assertEqual(get_model_rates.call_count, calls_after_first)

        self.client.get("/api/cost/get_model_rates/?refresh=1")
        self.assertGreater(get_model_rates.call_count, calls_after_first)

    def test_invalid_month_is_400(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/cost/get_model_rates/?month=nope").status_code, 400)


class FlagAutomatedChatsCommandTests(TestCase):
    """ENG-149/150: a scripted caller hit the live chat endpoint with the same
    message every ~6 minutes for weeks, each time as a brand-new chat_id --
    corrupting both the dashboard's conversation count and the AI-generated
    monthly insights (which sample the most-recent chats and can be crowded
    out entirely by a high-frequency bot). This command detects that exact
    signature after the fact: the same normalized opening message spiking
    across many distinct chats within one hour is not what a real batch of
    shoppers looks like."""

    def _chat_with_message(self, chat_id, content, when, model="claude-haiku-4-5"):
        chat = Chat.objects.create(chat_id=chat_id, model=model)
        Chat.objects.filter(pk=chat.pk).update(timestamp=when)
        msg = Message.objects.create(
            chat=chat, content=content, llm_formatted_message="{}",
            returned_content="", llm_formatted_returned_message="{}",
            tokens_in=0, tokens_out=0, model=model,
        )
        # Message.timestamp is auto_now_add=True, which silently ignores any
        # explicit value passed to .create() -- must override post-creation,
        # same as Chat's timestamp above.
        Message.objects.filter(pk=msg.pk).update(timestamp=when)

    def test_flags_a_repeated_message_spike_within_the_hour(self):
        hour = _now().replace(minute=5, second=0, microsecond=0)
        for i in range(6):
            self._chat_with_message(f"bot-{i}", "Do you have any Pokemon booster boxes in stock?",
                                     hour + timedelta(minutes=i))

        call_command("flag_automated_chats")

        flagged = set(Chat.objects.filter(likely_automated=True).values_list("chat_id", flat=True))
        self.assertEqual(flagged, {f"bot-{i}" for i in range(6)})

    def test_does_not_flag_at_or_below_the_threshold(self):
        hour = _now().replace(minute=5, second=0, microsecond=0)
        for i in range(5):
            self._chat_with_message(f"ok-{i}", "do you have destined rivals booster box in stock?",
                                     hour + timedelta(minutes=i))

        call_command("flag_automated_chats")

        self.assertEqual(Chat.objects.filter(likely_automated=True).count(), 0)

    def test_matching_is_case_and_whitespace_insensitive(self):
        hour = _now().replace(minute=5, second=0, microsecond=0)
        texts = ["Booster box?", "  booster   box?  ", "BOOSTER BOX?", "booster box?", "Booster Box?", "booster box? "]
        for i, text in enumerate(texts):
            self._chat_with_message(f"variant-{i}", text, hour + timedelta(minutes=i))

        call_command("flag_automated_chats")

        self.assertEqual(Chat.objects.filter(likely_automated=True).count(), 6)

    def test_does_not_flag_different_messages_even_in_large_volume(self):
        hour = _now().replace(minute=5, second=0, microsecond=0)
        for i in range(10):
            self._chat_with_message(f"real-{i}", f"unique question {i}", hour + timedelta(minutes=i))

        call_command("flag_automated_chats")

        self.assertEqual(Chat.objects.filter(likely_automated=True).count(), 0)

    def test_does_not_flag_across_different_hours(self):
        base = _now().replace(minute=5, second=0, microsecond=0)
        for i in range(3):
            self._chat_with_message(f"h1-{i}", "same question", base + timedelta(minutes=i))
        for i in range(3):
            self._chat_with_message(f"h2-{i}", "same question", base + timedelta(hours=2, minutes=i))

        call_command("flag_automated_chats")

        self.assertEqual(Chat.objects.filter(likely_automated=True).count(), 0)

    def test_dry_run_makes_no_changes(self):
        hour = _now().replace(minute=5, second=0, microsecond=0)
        for i in range(6):
            self._chat_with_message(f"dry-{i}", "same spammy question",
                                     hour + timedelta(minutes=i))

        call_command("flag_automated_chats", "--dry-run")

        self.assertEqual(Chat.objects.filter(likely_automated=True).count(), 0)

    def test_is_idempotent_across_repeated_runs(self):
        hour = _now().replace(minute=5, second=0, microsecond=0)
        for i in range(6):
            self._chat_with_message(f"idem-{i}", "same spammy question",
                                     hour + timedelta(minutes=i))

        call_command("flag_automated_chats")
        first_run_flagged = set(Chat.objects.filter(likely_automated=True).values_list("chat_id", flat=True))
        call_command("flag_automated_chats")
        second_run_flagged = set(Chat.objects.filter(likely_automated=True).values_list("chat_id", flat=True))

        self.assertEqual(first_run_flagged, second_run_flagged)
        self.assertEqual(len(first_run_flagged), 6)

    def test_ignores_chats_with_no_messages(self):
        chat = Chat.objects.create(chat_id="empty-chat", model="claude-haiku-4-5")
        Chat.objects.filter(pk=chat.pk).update(timestamp=_now())

        call_command("flag_automated_chats")  # must not raise

        self.assertFalse(Chat.objects.get(chat_id="empty-chat").likely_automated)

    def test_manual_admin_override_is_not_reverted_by_a_clean_rerun(self):
        chat = Chat.objects.create(chat_id="manually-flagged", model="claude-haiku-4-5", likely_automated=True)
        Chat.objects.filter(pk=chat.pk).update(timestamp=_now())
        Message.objects.create(
            chat=chat, content="a totally normal one-off question", llm_formatted_message="{}",
            returned_content="", llm_formatted_returned_message="{}",
            tokens_in=0, tokens_out=0, model="claude-haiku-4-5", timestamp=_now(),
        )

        call_command("flag_automated_chats")

        self.assertTrue(Chat.objects.get(chat_id="manually-flagged").likely_automated)


class SsoLoginTests(TestCase):
    """cost_management/sso.py -- the AOP-dashboard login bridge."""

    SECRET = "test-sso-secret"

    def setUp(self):
        self.user = User.objects.create_user(username="operator", password="pw")

    def _sign(self, exp):
        return hmac.new(self.SECRET.encode(), str(exp).encode(), hashlib.sha256).hexdigest()

    @override_settings(COSTAPP_SSO_SECRET=SECRET, COSTAPP_SSO_USERNAME="operator",
                        COST_APP_PUBLIC_URL="https://cost.example.com")
    def test_valid_token_logs_in_and_redirects_to_the_requested_chat(self):
        exp = int(time.time()) + 120
        token = self._sign(exp)

        resp = self.client.get(f"/api/cost/sso_login/?token={token}&exp={exp}&redirect=/chats?chat=abc-123")

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "https://cost.example.com/chats?chat=abc-123")
        # The session cookie set by this response now authenticates us, same
        # as a normal POST /login/ would.
        self.assertTrue(self.client.get("/api/cost/auth-check/").json()["authenticated"])

    @override_settings(COSTAPP_SSO_SECRET=SECRET, COSTAPP_SSO_USERNAME="operator",
                        COST_APP_PUBLIC_URL="https://cost.example.com")
    def test_missing_redirect_param_defaults_to_the_chat_list(self):
        exp = int(time.time()) + 120
        token = self._sign(exp)

        resp = self.client.get(f"/api/cost/sso_login/?token={token}&exp={exp}")

        self.assertEqual(resp["Location"], "https://cost.example.com/chats")

    @override_settings(COSTAPP_SSO_SECRET=SECRET, COSTAPP_SSO_USERNAME="operator",
                        COST_APP_PUBLIC_URL="https://cost.example.com")
    def test_expired_token_is_rejected(self):
        exp = int(time.time()) - 5
        token = self._sign(exp)

        resp = self.client.get(f"/api/cost/sso_login/?token={token}&exp={exp}")

        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self.client.get("/api/cost/auth-check/").json()["authenticated"])

    @override_settings(COSTAPP_SSO_SECRET=SECRET, COSTAPP_SSO_USERNAME="operator")
    def test_bad_signature_is_rejected(self):
        exp = int(time.time()) + 120

        resp = self.client.get(f"/api/cost/sso_login/?token=0000deadbeef&exp={exp}")

        self.assertEqual(resp.status_code, 403)

    @override_settings(COSTAPP_SSO_SECRET="", COSTAPP_SSO_USERNAME="operator")
    def test_unconfigured_secret_rejects_every_token(self):
        resp = self.client.get("/api/cost/sso_login/?token=whatever&exp=9999999999")

        self.assertEqual(resp.status_code, 403)

    @override_settings(COSTAPP_SSO_SECRET=SECRET, COSTAPP_SSO_USERNAME="nobody-configured")
    def test_username_with_no_matching_user_is_rejected(self):
        exp = int(time.time()) + 120
        token = self._sign(exp)

        resp = self.client.get(f"/api/cost/sso_login/?token={token}&exp={exp}")

        self.assertEqual(resp.status_code, 403)

    @override_settings(COSTAPP_SSO_SECRET=SECRET, COSTAPP_SSO_USERNAME="operator",
                        COST_APP_PUBLIC_URL="https://cost.example.com")
    def test_external_redirect_target_is_ignored_not_followed(self):
        """The redirect param must stay inside this app -- never an open redirect."""
        exp = int(time.time()) + 120
        token = self._sign(exp)

        resp = self.client.get(
            f"/api/cost/sso_login/?token={token}&exp={exp}&redirect=https://evil.example.com/phish"
        )

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "https://cost.example.com/chats")


class FlagProbeChatsTests(TestCase):
    """ENG-164: the AOP monitor's retired synthetic probe asked the same
    question every ~6 minutes as a new chat. The chatbot's repeat-message
    breaker let exactly five an hour through, which flag_automated_chats
    (more than five an hour) never caught, so they filled the chat summary.
    flag_probe_chats (and migration 0016) flag them by shape instead."""

    PROBE = "Do you have any Pokemon booster boxes in stock?"

    def setUp(self):
        from datetime import datetime, timezone as dt_timezone
        self.after_probe_started = datetime(2026, 9, 20, 12, 0, tzinfo=dt_timezone.utc)
        self.before_probe_started = datetime(2026, 9, 1, 12, 0, tzinfo=dt_timezone.utc)

    def _chat(self, chat_id, contents, when):
        chat = Chat.objects.create(chat_id=chat_id, model="claude-haiku-4-5")
        Chat.objects.filter(pk=chat.pk).update(timestamp=when)
        for content in contents:
            msg = Message.objects.create(
                chat=chat, content=content, llm_formatted_message="{}",
                returned_content="", llm_formatted_returned_message="{}",
                tokens_in=0, tokens_out=0, model="claude-haiku-4-5",
            )
            Message.objects.filter(pk=msg.pk).update(timestamp=when)

    def flagged(self):
        return set(Chat.objects.filter(likely_automated=True).values_list("chat_id", flat=True))

    def test_flags_single_turn_probe_chats_including_multi_call_turns(self):
        self._chat("probe-1", [self.PROBE], self.after_probe_started)
        # One probe turn logged as two LLM calls (tool use, then the answer).
        self._chat("probe-2", [self.PROBE, self.PROBE], self.after_probe_started)

        call_command("flag_probe_chats")

        self.assertEqual(self.flagged(), {"probe-1", "probe-2"})

    def test_keeps_a_shopper_who_asked_the_question_and_kept_talking(self):
        self._chat("shopper", [self.PROBE, "what about Surging Sparks?"], self.after_probe_started)

        call_command("flag_probe_chats")

        self.assertEqual(self.flagged(), set())

    def test_keeps_other_wording_and_chats_from_before_the_probe_existed(self):
        self._chat("reworded", ["do you have any pokemon booster boxes in stock"], self.after_probe_started)
        self._chat("early", [self.PROBE], self.before_probe_started)

        call_command("flag_probe_chats")

        self.assertEqual(self.flagged(), set())

    def test_dry_run_changes_nothing(self):
        self._chat("probe-1", [self.PROBE], self.after_probe_started)

        call_command("flag_probe_chats", "--dry-run")

        self.assertEqual(self.flagged(), set())

    def test_flagged_probe_chats_disappear_from_the_chat_summary(self):
        self._chat("probe-1", [self.PROBE], self.after_probe_started)
        self._chat("real-1", ["do you have charizard?"], self.after_probe_started)
        user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(user)

        call_command("flag_probe_chats")
        response = self.client.get("/api/cost/get_chat_ids/?limit=100")

        self.assertEqual({c["chat_id"] for c in response.json()["results"]}, {"real-1"})

    def test_migration_flags_probe_chats(self):
        import importlib
        from django.apps import apps as live_apps
        migration = importlib.import_module("cost_management.migrations.0016_flag_aop_probe_chats")
        from cost_management.models import CostMethodologyChange
        self._chat("probe-1", [self.PROBE], self.after_probe_started)

        migration.flag_probe_chats(live_apps, None)
        migration.flag_probe_chats(live_apps, None)  # idempotent

        self.assertEqual(self.flagged(), {"probe-1"})
        self.assertEqual(
            CostMethodologyChange.objects.filter(date="2026-09-25", category="measurement_fix").count(), 1
        )


class AnthropicDateBoundsAndRateFallbackTests(TestCase):
    """Verifies that all Anthropic API queries explicitly specify ending_at > starting_at
    (preventing the 400 Invalid date range error on the 1st of the month), and that
    day-1 rate derivation falls back to the previous month's established rates."""

    def test_month_bounds_normal_month(self):
        from .llm_provider_adapter_implementations import _month_bounds
        y, m, start, end = _month_bounds(2026, 10)
        self.assertEqual(y, 2026)
        self.assertEqual(m, 10)
        self.assertEqual(start, "2026-10-01T00:00:00Z")
        self.assertEqual(end, "2026-11-01T00:00:00Z")
        self.assertGreater(end, start)

    def test_month_bounds_december_rollover(self):
        from .llm_provider_adapter_implementations import _month_bounds
        y, m, start, end = _month_bounds(2026, 12)
        self.assertEqual(y, 2026)
        self.assertEqual(m, 12)
        self.assertEqual(start, "2026-12-01T00:00:00Z")
        self.assertEqual(end, "2027-01-01T00:00:00Z")
        self.assertGreater(end, start)

    def test_adapter_calls_include_ending_at(self):
        from .llm_provider_adapter_implementations import AnthropicAdapter
        adapter = AnthropicAdapter()
        mock_resp = MagicMock(status_code=200, json=lambda: {"data": []})

        # Test get_tokens
        with patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=mock_resp) as mock_get:
            adapter.get_tokens(year=2026, month=10)
            params = mock_get.call_args.kwargs["params"]
            self.assertIn("starting_at", params)
            self.assertIn("ending_at", params)
            self.assertGreater(params["ending_at"], params["starting_at"])

        # Test get_model_rates (hits cost_report and usage_report)
        with patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=mock_resp) as mock_get:
            adapter.get_model_rates(year=2026, month=10)
            self.assertEqual(mock_get.call_count, 2)
            for call in mock_get.call_args_list:
                params = call.kwargs["params"]
                self.assertIn("starting_at", params)
                self.assertIn("ending_at", params)
                self.assertGreater(params["ending_at"], params["starting_at"])

        # Test get_usage_by_key
        with patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=mock_resp) as mock_get:
            adapter.get_usage_by_key(year=2026, month=10)
            usage_params = mock_get.call_args_list[0].kwargs["params"]
            self.assertIn("starting_at", usage_params)
            self.assertIn("ending_at", usage_params)
            self.assertGreater(usage_params["ending_at"], usage_params["starting_at"])

        # Test get_cost (with mocked rates)
        rates_resp = {"rates": {"m": {"input": 0.001, "output": 0.002}}}
        with patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=mock_resp) as mock_get:
            adapter.get_cost(year=2026, month=10, rates_resp=rates_resp)
            params = mock_get.call_args.kwargs["params"]
            self.assertIn("starting_at", params)
            self.assertIn("ending_at", params)
            self.assertGreater(params["ending_at"], params["starting_at"])

    def test_get_cost_falls_back_to_prev_month_rates_on_day_one(self):
        from .llm_provider_adapter_implementations import AnthropicAdapter
        from datetime import datetime
        today = datetime.today()
        adapter = AnthropicAdapter()

        usage_resp = MagicMock(status_code=200, json=lambda: {"data": [{
            "starting_at": f"{today.year}-{today.month:02d}-01T00:00:00Z",
            "results": [{"model": "haiku", "uncached_input_tokens": 1000, "output_tokens": 500}],
        }]})

        prev_y, prev_m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        fallback_rates = {"rates": {"haiku": {"input": 0.001, "output": 0.002, "cache_creation": 0, "cache_read": 0}}}

        def mock_rates(year=None, month=None):
            if year == today.year and month == today.month:
                return {"rates": {}}
            return fallback_rates

        with patch.object(adapter, "get_model_rates", side_effect=mock_rates), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=usage_resp):
            result = adapter.get_cost(year=today.year, month=today.month)

        # 1000 * 0.001 + 500 * 0.002 = 1.0 + 1.0 = 2.0
        self.assertEqual(result["costs"][0]["total_cost"], 2.0)

    def test_stats_views_rates_for_falls_back_to_prev_month(self):
        from .stats_views import _rates_for
        from .month_utils import current_month_start, prev_month
        current = current_month_start()
        prev = prev_month(current)

        def mock_rates_resp(month_start):
            if month_start == current:
                return {"rates": {}}
            return {"rates": {"model-a": {"input": 0.0001, "output": 0.0002}}}

        with patch("cost_management.stats_views._rates_resp_for", side_effect=mock_rates_resp):
            rates = _rates_for(current)
            self.assertEqual(rates, {"model-a": {"input": 0.0001, "output": 0.0002}})

    def test_get_model_rates_falls_back_on_date_range_error(self):
        from .llm_provider_adapter_implementations import AnthropicAdapter
        adapter = AnthropicAdapter()
        date_err_resp = MagicMock(
            status_code=400,
            text='{"type":"error","error":{"type":"invalid_request_error","message":"Invalid date range: ending date must be after starting date"}}'
        )
        prev_cost_resp = MagicMock(status_code=200, json=lambda: {
            "data": [{
                "starting_at": "2026-09-01T00:00:00Z",
                "results": [{"model": "haiku", "cost_type": "tokens", "token_type": "uncached_input_tokens", "amount": 100}]
            }]
        })
        prev_usage_resp = MagicMock(status_code=200, json=lambda: {
            "data": [{
                "starting_at": "2026-09-01T00:00:00Z",
                "results": [{"model": "haiku", "uncached_input_tokens": 100000}]
            }]
        })

        def mock_get(url, params=None, headers=None):
            # For 2026-10 (current month on day 1), return 400 Invalid date range
            if params and "2026-10-01" in params.get("starting_at", ""):
                return date_err_resp
            # For 2026-09 (fallback), return valid responses
            if "cost_report" in url:
                return prev_cost_resp
            return prev_usage_resp

        with patch("cost_management.llm_provider_adapter_implementations.requests.get", side_effect=mock_get):
            result = adapter.get_model_rates(year=2026, month=10)
            self.assertIn("rates", result)
            self.assertIn("haiku", result["rates"])
            self.assertAlmostEqual(result["rates"]["haiku"]["input"], 0.00001)

    def test_get_tokens_handles_date_range_error(self):
        from .llm_provider_adapter_implementations import AnthropicAdapter
        adapter = AnthropicAdapter()
        date_err_resp = MagicMock(
            status_code=400,
            text='{"type":"error","error":{"type":"invalid_request_error","message":"Invalid date range: ending date must be after starting date"}}'
        )

        with patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=date_err_resp):
            result = adapter.get_tokens(year=2026, month=10)
            self.assertEqual(result["tokens"], [])
            self.assertEqual(result["cache"]["creation_tokens"], 0)
            self.assertEqual(result["cache"]["read_tokens"], 0)
            self.assertIsNone(result["cache"]["hit_rate"])
            self.assertNotIn("error", result)

    def test_get_cost_handles_date_range_error(self):
        from .llm_provider_adapter_implementations import AnthropicAdapter
        adapter = AnthropicAdapter()
        date_err_resp = MagicMock(
            status_code=400,
            text='{"type":"error","error":{"type":"invalid_request_error","message":"Invalid date range: ending date must be after starting date"}}'
        )
        fallback_rates = {"rates": {"haiku": {"input": 0.001, "output": 0.002}}}

        with patch.object(adapter, "get_model_rates", return_value=fallback_rates), \
             patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=date_err_resp):
            result = adapter.get_cost(year=2026, month=10)
            self.assertEqual(result["costs"], [])
            self.assertEqual(result["monthly_average_cost"], 0.0)
            self.assertNotIn("error", result)

    def test_get_usage_by_key_handles_date_range_error(self):
        from .llm_provider_adapter_implementations import AnthropicAdapter
        adapter = AnthropicAdapter()
        date_err_resp = MagicMock(
            status_code=400,
            text='{"type":"error","error":{"type":"invalid_request_error","message":"Invalid date range: ending date must be after starting date"}}'
        )

        with patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=date_err_resp):
            result = adapter.get_usage_by_key(year=2026, month=10)
            self.assertEqual(result["keys"], [])
            self.assertNotIn("error", result)

    def test_build_stats_falls_back_when_current_month_rates_error(self):
        from .stats_views import _build_stats
        from .month_utils import current_month_start, prev_month
        current = current_month_start()
        prev = prev_month(current)

        def mock_rates_resp(month_start):
            if month_start == current:
                return {"error": '{"type":"error","error":{"type":"invalid_request_error","message":"Invalid date range: ending date must be after starting date"}}'}
            return {"rates": {"haiku": {"input": 0.001, "output": 0.002}}}

        def mock_spend(month_start, rates_resp):
            return 0.0, [], None

        def mock_tokens(month_start):
            return 0, 0, [], {"creation_tokens": 0, "read_tokens": 0, "hit_rate": None}, None

        with patch("cost_management.stats_views._rates_resp_for", side_effect=mock_rates_resp), \
             patch("cost_management.stats_views._spend_for", side_effect=mock_spend), \
             patch("cost_management.stats_views._tokens_for", side_effect=mock_tokens):
            stats = _build_stats(current)
            self.assertIsNone(stats["cost_source_error"])



class ApiLoginRequiredTests(TestCase):
    """Protected API views must answer 401 JSON, not a 302 to /accounts/login/
    (which doesn't exist here, so fetch() followed it into a misleading 404)."""

    def test_unauthenticated_returns_401_json_without_redirect(self):
        resp = self.client.get("/api/cost/monthly_stats/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"error": "unauthenticated"})
        self.assertNotIn("Location", resp)

    def test_authenticated_passes_through(self):
        from django.contrib.auth.models import User
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        self.assertNotEqual(self.client.get("/api/cost/monthly_stats/").status_code, 401)


class LogoutEndpointTests(TestCase):
    """Tests for the /api/cost/logout/ endpoint."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="secretpassword")

    def test_logout_clears_session_and_returns_success(self):
        self.client.login(username="testuser", password="secretpassword")

        # Verify authenticated before logout
        check_before = self.client.get("/api/cost/auth-check/")
        self.assertTrue(check_before.json()["authenticated"])

        # Perform logout
        resp = self.client.post("/api/cost/logout/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True})

        # Verify unauthenticated after logout
        check_after = self.client.get("/api/cost/auth-check/")
        self.assertFalse(check_after.json()["authenticated"])

        # Protected endpoints now return 401
        protected_resp = self.client.get("/api/cost/monthly_stats/")
        self.assertEqual(protected_resp.status_code, 401)
        self.assertEqual(protected_resp.json(), {"error": "unauthenticated"})

    def test_logout_when_not_logged_in_is_idempotent(self):
        resp = self.client.post("/api/cost/logout/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"success": True})


class ShadowtestTrafficExclusionTests(TestCase):
    """Verifies that any chats containing 'shadowtest' in their chat_id
    are fully excluded from metrics, conversation counts, get_chat_ids,
    and are automatically flagged as likely_automated."""

    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(self.user)

    def test_real_chats_excludes_shadowtest_chat_ids(self):
        from .month_utils import real_chats
        c_real = Chat.objects.create(chat_id="customer-1", model="claude-haiku-4-5")
        Chat.objects.create(chat_id="bot-probe", model="claude-haiku-4-5", likely_automated=True)
        Chat.objects.create(chat_id="conv-shadowtest-01", model="claude-haiku-4-5", likely_automated=False)
        Chat.objects.create(chat_id="ShadowTest_Upper", model="claude-haiku-4-5", likely_automated=False)

        visible = list(real_chats(Chat.objects.all()).values_list("chat_id", flat=True))
        self.assertEqual(visible, [c_real.chat_id])

    def test_log_message_auto_flags_shadowtest(self):
        payload = make_log_message_payload("eval-shadowtest-77")
        resp = self.client.post(
            "/api/cost/log_message/",
            data=json.dumps(payload),
            content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        chat = Chat.objects.get(chat_id="eval-shadowtest-77")
        self.assertTrue(chat.likely_automated)

    def test_get_chat_ids_excludes_shadowtest(self):
        Chat.objects.create(chat_id="real-shopper", model="claude-haiku-4-5")
        Chat.objects.create(chat_id="qa-shadowtest-1", model="claude-haiku-4-5")
        Chat.objects.create(chat_id="qa-shadowtest-2", model="claude-haiku-4-5", likely_automated=True)

        resp = self.client.get("/api/cost/get_chat_ids/?limit=50")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        ids = [c["chat_id"] for c in data["results"]]
        self.assertEqual(ids, ["real-shopper"])

    def test_get_avg_metrics_exclude_shadowtest(self):
        # Real chat with score 100 and 200 tokens
        Chat.objects.create(
            chat_id="real-shopper-eval",
            model="claude-haiku-4-5",
            evaluation_score=100,
            tokens_in=200,
            tokens_out=100,
        )
        # Shadowtest chat with score 10 and 50,000 tokens
        Chat.objects.create(
            chat_id="batch-shadowtest-run",
            model="claude-haiku-4-5",
            evaluation_score=10,
            tokens_in=50000,
            tokens_out=50000,
        )

        resp_eval = self.client.get("/api/cost/get_avg_eval_score/?period=daily")
        self.assertEqual(resp_eval.status_code, 200)
        self.assertEqual(resp_eval.json()["average_eval_score"], 100.0)

        resp_tok_in = self.client.get("/api/cost/get_avg_tokens_in/?period=daily")
        self.assertEqual(resp_tok_in.status_code, 200)
        self.assertEqual(resp_tok_in.json()["average_tokens_in"], 200.0)

        resp_tok_out = self.client.get("/api/cost/get_avg_tokens_out/?period=daily")
        self.assertEqual(resp_tok_out.status_code, 200)
        self.assertEqual(resp_tok_out.json()["average_tokens_out"], 100.0)

    def test_migration_0017_flags_existing_shadowtest_chats(self):
        import importlib
        migration_mod = importlib.import_module("cost_management.migrations.0017_flag_shadowtest_chats")
        flag_shadowtest_chats = migration_mod.flag_shadowtest_chats
        from django.apps import apps

        chat = Chat.objects.create(
            chat_id="historical-shadowtest-data",
            model="claude-haiku-4-5",
            likely_automated=False
        )
        self.assertFalse(Chat.objects.get(chat_id=chat.chat_id).likely_automated)

        flag_shadowtest_chats(apps, None)
        self.assertTrue(Chat.objects.get(chat_id=chat.chat_id).likely_automated)


class AutoAuditAndBatchEvaluationTests(TestCase):
    """Verifies smart auto-audit filtering, batch evaluation endpoint,
    and the auto_audit_chats management command."""

    def setUp(self):
        self.user = User.objects.create_user(username="auditor", password="pw")
        self.client.force_login(self.user)

    @patch("anthropic.Anthropic")
    def test_score_single_chat_success(self, mock_anthropic_class):
        from .views import score_single_chat
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text="92")]
        mock_client.messages.create.return_value = mock_resp

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            chat = Chat.objects.create(chat_id="test-score-1", model="claude-haiku-4-5")
            Message.objects.create(
                chat=chat,
                content="Do you have Charizard?",
                returned_content="Yes we do!",
                tokens_in=50,
                tokens_out=20,
                model="claude-haiku-4-5",
            )

            score = score_single_chat(chat)
            self.assertEqual(score, 92)
            chat.refresh_from_db()
            self.assertEqual(chat.evaluation_score, 92)

    def test_should_auto_audit_chat_filtering(self):
        from .views import should_auto_audit_chat
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            # Bot should not be audited
            c_bot = Chat.objects.create(chat_id="bot-test", model="claude-haiku-4-5", likely_automated=True)
            self.assertFalse(should_auto_audit_chat(c_bot))

            # Shadowtest should not be audited
            c_shadow = Chat.objects.create(chat_id="conv-shadowtest-9", model="claude-haiku-4-5")
            self.assertFalse(should_auto_audit_chat(c_shadow))

            # Already scored should not be audited
            c_scored = Chat.objects.create(chat_id="real-scored", model="claude-haiku-4-5", evaluation_score=90)
            self.assertFalse(should_auto_audit_chat(c_scored))

            # Real chat with products shown qualifies
            c_prod = Chat.objects.create(chat_id="real-prod", model="claude-haiku-4-5")
            self.assertTrue(should_auto_audit_chat(c_prod, products_shown={"primary": [{"name": "Charizard"}]}))

    @patch("cost_management.views.score_single_chat")
    def test_batch_evaluate_endpoint(self, mock_score):
        mock_score.return_value = 95
        c1 = Chat.objects.create(chat_id="batch-c1", model="claude-haiku-4-5")
        c2 = Chat.objects.create(chat_id="batch-c2", model="claude-haiku-4-5")
        Message.objects.create(chat=c1, content="q1", returned_content="a1", tokens_in=10, tokens_out=10, model="claude-haiku-4-5")
        Message.objects.create(chat=c2, content="q2", returned_content="a2", tokens_in=10, tokens_out=10, model="claude-haiku-4-5")

        resp = self.client.post(
            "/api/cost/batch_evaluate/",
            data=json.dumps({"limit": 10}),
            content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["audited_count"], 2)
        self.assertIn("estimated_cost_usd", data)

    @patch("cost_management.views.score_single_chat")
    def test_batch_evaluate_excludes_pre_june_2026_conversations(self, mock_score):
        mock_score.return_value = 90
        import datetime
        from django.utils import timezone
        c_old = Chat.objects.create(chat_id="batch-old", model="claude-haiku-4-5")
        Chat.objects.filter(pk=c_old.pk).update(timestamp=timezone.make_aware(datetime.datetime(2026, 5, 31, 23, 59, 59)))
        c_new = Chat.objects.create(chat_id="batch-new", model="claude-haiku-4-5")
        Chat.objects.filter(pk=c_new.pk).update(timestamp=timezone.make_aware(datetime.datetime(2026, 6, 1, 12, 0, 0)))

        resp = self.client.post(
            "/api/cost/batch_evaluate/",
            data=json.dumps({"limit": 10}),
            content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["audited_count"], 1)
        self.assertEqual(data["results"][0]["chat_id"], "batch-new")


    def test_batch_evaluate_unauthenticated(self):
        self.client.logout()
        resp = self.client.post("/api/cost/batch_evaluate/", data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 401)

    def test_batch_evaluate_zero_unaudited(self):
        Chat.objects.create(chat_id="already-scored", model="claude-haiku-4-5", evaluation_score=98)
        resp = self.client.post("/api/cost/batch_evaluate/", data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["audited_count"], 0)
        self.assertEqual(data["results"], [])

    def test_should_auto_audit_respects_daily_cap(self):
        from .views import should_auto_audit_chat, DAILY_AUTO_AUDIT_CAP
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            # Create 30 chats scored today
            for i in range(DAILY_AUTO_AUDIT_CAP):
                Chat.objects.create(
                    chat_id=f"cap-chat-{i}",
                    model="claude-haiku-4-5",
                    evaluation_score=95
                )

            # New chat arrives
            c_new = Chat.objects.create(chat_id="new-chat-over-cap", model="claude-haiku-4-5")
            self.assertFalse(should_auto_audit_chat(c_new, products_shown={"primary": [{"name": "Pikachu"}]}))


class AnthropicRateLimitAndRetryTests(TestCase):
    """Verifies that AnthropicAdapter retries on HTTP 429/529 with backoff,
    and stats_views does not cache transient rate-limit errors."""

    def test_anthropic_get_retries_on_429_and_succeeds(self):
        from .llm_provider_adapter_implementations import _anthropic_get
        rate_limit_resp = MagicMock(
            status_code=429,
            headers={"retry-after": "1"},
            text='{"type":"error","error":{"type":"rate_limit_error","message":"You exceeded your rate limit."}}'
        )
        success_resp = MagicMock(status_code=200, json=lambda: {"data": []})
        sleep_mock = MagicMock()

        with patch("cost_management.llm_provider_adapter_implementations.requests.get", side_effect=[rate_limit_resp, success_resp]):
            resp = _anthropic_get("https://api.anthropic.com/v1/organizations/usage_report/messages", {}, {}, max_retries=3, sleep_fn=sleep_mock)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(sleep_mock.call_count, 1)
            self.assertGreaterEqual(sleep_mock.call_args[0][0], 1.0)

    def test_anthropic_get_exceeds_max_retries(self):
        from .llm_provider_adapter_implementations import _anthropic_get
        rate_limit_resp = MagicMock(
            status_code=429,
            headers={"retry-after": "0.5"},
            text='{"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded."}}'
        )
        sleep_mock = MagicMock()

        with patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=rate_limit_resp):
            resp = _anthropic_get("https://api.anthropic.com/v1/organizations/usage_report/messages", {}, {}, max_retries=2, sleep_fn=sleep_mock)
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(sleep_mock.call_count, 2)

    def test_spend_for_and_tokens_for_do_not_cache_rate_limit_errors(self):
        from django.core.cache import cache
        from .stats_views import _spend_for, _tokens_for, _raw_usage_by_key
        from .month_utils import current_month_start, prev_month
        cache.clear()

        m = prev_month(current_month_start())
        month_str = f"{m.year:04d}-{m.month:02d}"

        rate_limit_resp = MagicMock(
            status_code=429,
            headers={"retry-after": "0"},
            text='{"type":"error","error":{"type":"rate_limit_error","message":"You exceeded your rate limit."}}'
        )
        with patch("cost_management.llm_provider_adapter_implementations.requests.get", return_value=rate_limit_resp):
            # spend_for returns (total, daily, err)
            total, daily, spend_err = _spend_for(m, rates_resp={})
            self.assertIsNotNone(spend_err)
            self.assertIn("rate_limit_error", spend_err)
            self.assertIsNone(cache.get(f"spend_for:{month_str}"))

            # tokens_for returns (in, out, daily, cache_info, err)
            t_in, t_out, t_daily, c_info, token_err = _tokens_for(m)
            self.assertIsNotNone(token_err)
            self.assertIn("rate_limit_error", token_err)
            self.assertIsNone(cache.get(f"tokens_for:{month_str}"))

            # raw_usage_by_key returns dict with "error"
            usage = _raw_usage_by_key(m)
            self.assertIn("error", usage)
            self.assertIsNone(cache.get(f"raw_usage_by_key:{month_str}"))






def make_attribution_payload(chat_id="conv-attr-1", order_id="1001", **overrides):
    payload = {
        "chat_id": chat_id,
        "shop": "test-shop.myshopify.com",
        "order_id": order_id,
        "attribution_type": "influenced",
        "influenced_revenue": "89.99",
        "order_total": "104.99",
        "currency": "USD",
        "order_created_at": "2026-10-06T12:00:00Z",
        "surfaces": ["chat"],
        "influence_score": 0.85,
    }
    payload.update(overrides)
    return payload


class LogAttributionTests(TestCase):
    def test_happy_path_creates_row_and_links_chat(self):
        Chat.objects.create(chat_id="conv-attr-1", model="claude-haiku-4-5")
        response = self.client.post(
            "/api/cost/log_attribution/",
            data=json.dumps(make_attribution_payload()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")

        order = AttributedOrder.objects.get(shop="test-shop.myshopify.com", order_id="1001")
        self.assertEqual(order.chat.chat_id, "conv-attr-1")
        self.assertEqual(order.chat_id_raw, "conv-attr-1")
        self.assertEqual(float(order.influenced_revenue), 89.99)
        self.assertEqual(order.currency, "USD")
        self.assertEqual(order.surfaces, ["chat"])

    def test_replay_of_same_order_updates_instead_of_duplicating(self):
        first = make_attribution_payload()
        replay = make_attribution_payload(influenced_revenue="95.50", order_total="110.00")
        for payload in (first, replay):
            response = self.client.post(
                "/api/cost/log_attribution/",
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

        qs = AttributedOrder.objects.filter(shop="test-shop.myshopify.com", order_id="1001")
        self.assertEqual(qs.count(), 1)
        self.assertEqual(float(qs.get().influenced_revenue), 95.50)

    def test_unknown_chat_id_still_stored_unlinked(self):
        # Attribution must never lose revenue: a chat_id with no Chat row
        # stores with chat=NULL so commercial_impact can surface it as
        # `unlinked_orders` (join-key drift canary).
        response = self.client.post(
            "/api/cost/log_attribution/",
            data=json.dumps(make_attribution_payload(chat_id="never-logged")),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        order = AttributedOrder.objects.get(order_id="1001")
        self.assertIsNone(order.chat)
        self.assertEqual(order.chat_id_raw, "never-logged")

    def test_missing_chat_id_or_order_id_is_400(self):
        for payload in (
            make_attribution_payload(chat_id=""),
            make_attribution_payload(order_id=""),
        ):
            response = self.client.post(
                "/api/cost/log_attribution/",
                data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 400)

    def test_requires_post(self):
        self.assertEqual(self.client.get("/api/cost/log_attribution/").status_code, 405)

    def test_no_login_required_like_log_message(self):
        # Ingestion is server-to-server, fire-and-forget -- same shape as log_message.
        response = self.client.post(
            "/api/cost/log_attribution/",
            data=json.dumps(make_attribution_payload(order_id="2002")),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)


class CommercialImpactTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")
        self.this_month = _now().replace(day=1, hour=12, minute=0, second=0, microsecond=0)

    def tearDown(self):
        cache.clear()

    def _seed(self, chat_id, revenue, when=None, currency="USD", order_id=None):
        when = when or self.this_month.replace(day=10)
        Chat.objects.create(chat_id=chat_id, model="claude-haiku-4-5")
        AttributedOrder.objects.create(
            chat_id_raw=chat_id,
            chat=Chat.objects.get(chat_id=chat_id),
            shop="test-shop.myshopify.com",
            order_id=order_id or f"ord-{chat_id}",
            influenced_revenue=revenue,
            order_total=revenue,
            currency=currency,
            order_created_at=when,
        )

    def _patch_spend(self, spend):
        p = patch(
            "cost_management.stats_views._spend_for",
            return_value=(spend, [], None),
        )
        p.start()
        self.addCleanup(p.stop)

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/cost/commercial_impact/").status_code, 401)

    def test_four_numbers_and_methodology(self):
        self._patch_spend(10.0)
        self._seed("c1", 100, order_id="o1")
        self._seed("c2", 50, order_id="o2")
        Chat.objects.create(chat_id="c3-no-order", model="claude-haiku-4-5")
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/commercial_impact/").json()

        self.assertEqual(d["spend"], 10.0)
        self.assertEqual(d["influenced_revenue"], 150.0)
        self.assertEqual(d["revenue_per_dollar"], 15.0)
        self.assertEqual(d["conversations"], 3)
        self.assertEqual(d["converting_conversations"], 2)
        self.assertAlmostEqual(d["conversion_rate"], 2 / 3, places=4)
        self.assertEqual(d["currency"], "USD")
        self.assertFalse(d["mixed_currencies"])
        self.assertIn("Influenced revenue", d["methodology"])
        self.assertIn("data_as_of", d)

    def test_revenue_per_dollar_is_null_when_spend_is_zero(self):
        # Never a 0/0 artifact -- null renders as an em dash, not a lie.
        self._patch_spend(0)
        self._seed("c1", 100, order_id="o1")
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/commercial_impact/").json()
        self.assertIsNone(d["revenue_per_dollar"])
        self.assertEqual(d["influenced_revenue"], 100.0)

    def test_mixed_currencies_flagged_with_majority_currency(self):
        self._patch_spend(10.0)
        self._seed("c1", 200, currency="USD", order_id="o1")
        self._seed("c2", 50, currency="CAD", order_id="o2")
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/commercial_impact/").json()
        self.assertTrue(d["mixed_currencies"])
        self.assertEqual(d["currency"], "USD")

    def test_unlinked_orders_surfaced_as_canary(self):
        self._patch_spend(10.0)
        # No Chat row for ghost-chat: stored unlinked by log_attribution.
        AttributedOrder.objects.create(
            chat_id_raw="ghost-chat",
            chat=None,
            shop="test-shop.myshopify.com",
            order_id="o-ghost",
            influenced_revenue=25,
            order_total=25,
            currency="USD",
            order_created_at=self.this_month.replace(day=10),
        )
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/commercial_impact/").json()
        self.assertEqual(d["unlinked_orders"], 1)
        self.assertEqual(d["influenced_revenue"], 25.0)

    def test_invalid_month_param(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/cost/commercial_impact/?month=nope")
        self.assertEqual(response.status_code, 400)


class ReportRecommendationsTests(TestCase):
    """ENG-205: secret-gated monthly recommendations feed for the chatbot's
    monthly Slack report. Serves the frozen InsightsSnapshot recommendations
    for a month -- the same "where to invest next" data removed from the
    dashboard (PR #72). Fail-soft: no snapshot -> 200 with []."""

    def setUp(self):
        from .month_utils import current_month_start, prev_month
        self.prev_month = prev_month(current_month_start())
        self.prev_label = self.prev_month.strftime("%Y-%m")
        self.url = "/api/cost/report_recommendations/"
        # The endpoint must never depend on ambient env in tests.
        os.environ.pop("COSTAPP_REPORT_SECRET", None)

    def _snapshot(self, month, recommendations):
        from .models import InsightsSnapshot
        return InsightsSnapshot.objects.create(
            month=month,
            payload={
                "recommendations": recommendations,
                "generated_at": "2026-09-15T10:00:00+00:00",
            },
            conversations_analyzed=40,
        )

    def _recs(self):
        return [
            {
                "title": "Restock OP-05",
                "detail": "Shoppers keep asking",
                "impact": "high",
                "effort": "low",
                "addresses": "catalog gap",
                "evidence_count": 12,
                "examples": ["chat-1"],
            },
            {"detail": "x"},  # missing title -> dropped
            "not a dict",  # malformed -> dropped
        ]

    def test_missing_secret_env_returns_500(self):
        response = self.client.get(self.url, {"secret": "anything"})
        self.assertEqual(response.status_code, 500)

    def test_wrong_secret_returns_401(self):
        with patch.dict(os.environ, {"COSTAPP_REPORT_SECRET": "s3cr3t"}):
            response = self.client.get(self.url, {"secret": "wrong"})
            self.assertEqual(response.status_code, 401)

    def test_missing_secret_param_returns_401(self):
        with patch.dict(os.environ, {"COSTAPP_REPORT_SECRET": "s3cr3t"}):
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 401)

    def test_defaults_to_prior_month_snapshot(self):
        self._snapshot(self.prev_month, self._recs())
        with patch.dict(os.environ, {"COSTAPP_REPORT_SECRET": "s3cr3t"}):
            d = self.client.get(self.url, {"secret": "s3cr3t"}).json()
        self.assertEqual(d["month"], self.prev_label)
        self.assertEqual(len(d["recommendations"]), 1)
        rec = d["recommendations"][0]
        # Slack-safe shape: only the fields the report needs
        self.assertEqual(
            rec,
            {"title": "Restock OP-05", "detail": "Shoppers keep asking",
             "impact": "high", "effort": "low"},
        )
        self.assertEqual(d["generated_at"], "2026-09-15T10:00:00+00:00")

    def test_month_param_respected(self):
        from .month_utils import prev_month
        older = prev_month(self.prev_month)
        self._snapshot(older, [{"title": "Old rec", "detail": "d"}])
        self._snapshot(self.prev_month, [{"title": "New rec", "detail": "d"}])
        with patch.dict(os.environ, {"COSTAPP_REPORT_SECRET": "s3cr3t"}):
            d = self.client.get(
                self.url, {"secret": "s3cr3t", "month": older.strftime("%Y-%m")}
            ).json()
        self.assertEqual(d["month"], older.strftime("%Y-%m"))
        self.assertEqual(d["recommendations"][0]["title"], "Old rec")

    def test_invalid_month_returns_400(self):
        with patch.dict(os.environ, {"COSTAPP_REPORT_SECRET": "s3cr3t"}):
            response = self.client.get(self.url, {"secret": "s3cr3t", "month": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_no_snapshot_returns_empty_200(self):
        with patch.dict(os.environ, {"COSTAPP_REPORT_SECRET": "s3cr3t"}):
            response = self.client.get(self.url, {"secret": "s3cr3t"})
        self.assertEqual(response.status_code, 200)
        d = response.json()
        self.assertEqual(d["recommendations"], [])
        self.assertIsNone(d["generated_at"])


class CostCommentaryCitationTests(TestCase):
    """ENG-199 C1a: citation-carrying cost commentary. Every driver claim
    must cite a real changelog row or dashboard figure; _verify_citations
    checks deterministically (no LLM) and drops anything unverified."""

    def setUp(self):
        from datetime import date
        from .cost_commentary import _stat_metrics_table
        from .models import CostMethodologyChange
        self.stats = {
            "spend": {"total": 12.5, "prev_total": 10.0, "delta_pct": 25.0},
            "conversations": {"total": 200, "prev_total": 100, "delta_pct": 100.0},
            "per_conversation": {"cost": 0.0625, "prev_cost": 0.1, "cost_delta_pct": -37.5},
            "tokens": {"cache_hit_rate": 0.42},
        }
        self.change = CostMethodologyChange(
            date=date(2026, 9, 16),
            category="measurement_fix",
            description="Fixed double-counting of cache reads",
        )
        self.table = _stat_metrics_table(self.stats)

    def test_prompt_names_metrics_and_requires_citations(self):
        from .cost_commentary import _build_cost_commentary_prompt
        prompt = _build_cost_commentary_prompt(self.stats, [self.change], "September 2026")
        # citable metric table with exact figures
        self.assertIn("spend.total = 12.5", prompt)
        self.assertIn("conversations.delta_pct = 100.0", prompt)
        # changelog rows with exact date + raw category
        self.assertIn("2026-09-16 [measurement_fix]", prompt)
        # structured citation instruction
        self.assertIn('"citations"', prompt)
        self.assertIn("EMPTY citations array", prompt)
        self.assertIn("unexplained", prompt)

    def test_valid_changelog_citation_passes(self):
        from .cost_commentary import _verify_citations
        core = {"drivers": [{
            "type": "measurement_artifact",
            "description": "Cache double-count fixed",
            "citations": [{"kind": "changelog", "date": "2026-09-16", "category": "measurement_fix"}],
        }]}
        out = _verify_citations(core, self.stats, [self.change])
        d = out["drivers"][0]
        self.assertTrue(d["cited"])
        self.assertEqual(d["type"], "measurement_artifact")
        self.assertEqual(out["claims_total"], 1)
        self.assertEqual(out["claims_cited"], 1)
        self.assertEqual(out["grounding_rate"], 1.0)

    def test_changelog_citation_wrong_date_or_category_dropped(self):
        from .cost_commentary import _verify_citations
        for bad in [
            {"kind": "changelog", "date": "2026-09-17", "category": "measurement_fix"},
            {"kind": "changelog", "date": "2026-09-16", "category": "new_feature"},
        ]:
            core = {"drivers": [{
                "type": "measurement_artifact", "description": "x", "citations": [bad],
            }]}
            out = _verify_citations(core, self.stats, [self.change])
            d = out["drivers"][0]
            self.assertEqual(d["citations"], [])
            self.assertFalse(d["cited"])
            # no evidence -> no claim: downgraded
            self.assertEqual(d["type"], "unexplained")

    def test_valid_stat_citation_passes(self):
        from .cost_commentary import _verify_citations
        core = {"drivers": [{
            "type": "real_usage_change",
            "description": "Volume doubled",
            "citations": [{"kind": "stat", "metric": "conversations.total", "value": 200}],
        }]}
        out = _verify_citations(core, self.stats, [self.change])
        self.assertTrue(out["drivers"][0]["cited"])

    def test_stat_citation_wrong_value_or_metric_dropped(self):
        from .cost_commentary import _verify_citations
        for bad in [
            {"kind": "stat", "metric": "conversations.total", "value": 201},
            {"kind": "stat", "metric": "spend.bogus", "value": 12.5},
            {"kind": "bogus", "metric": "spend.total", "value": 12.5},
        ]:
            core = {"drivers": [{
                "type": "real_usage_change", "description": "x", "citations": [bad],
            }]}
            out = _verify_citations(core, self.stats, [self.change])
            self.assertFalse(out["drivers"][0]["cited"])
            self.assertEqual(out["drivers"][0]["type"], "unexplained")

    def test_float_tolerance(self):
        from .cost_commentary import _verify_citations
        core = {"drivers": [{
            "type": "real_usage_change", "description": "x",
            "citations": [{"kind": "stat", "metric": "per_conversation.cost", "value": 0.0625000001}],
        }]}
        out = _verify_citations(core, self.stats, [self.change])
        self.assertTrue(out["drivers"][0]["cited"])

    def test_legacy_changelog_date_converted(self):
        from .cost_commentary import _verify_citations
        core = {"drivers": [{
            "type": "measurement_artifact",
            "description": "Old payload",
            "changelog_date": "2026-09-16",
        }]}
        out = _verify_citations(core, self.stats, [self.change])
        d = out["drivers"][0]
        self.assertTrue(d["cited"])
        self.assertEqual(d["citations"][0]["kind"], "changelog")

    def test_grounding_counts(self):
        from .cost_commentary import _verify_citations
        core = {"drivers": [
            {"type": "real_usage_change", "description": "a",
             "citations": [{"kind": "stat", "metric": "spend.total", "value": 12.5}]},
            {"type": "unexplained", "description": "b", "citations": []},
            {"type": "measurement_artifact", "description": "c",
             "citations": [{"kind": "changelog", "date": "2099-01-01", "category": "incident"}]},
        ]}
        out = _verify_citations(core, self.stats, [self.change])
        self.assertEqual(out["claims_total"], 3)
        self.assertEqual(out["claims_cited"], 1)
        self.assertEqual(out["grounding_rate"], round(1 / 3, 3))
        # the invented changelog date is dropped and the driver downgraded
        self.assertEqual(out["drivers"][2]["type"], "unexplained")

    def test_commentary_for_applies_verification(self):
        from . import cost_commentary as cc
        from unittest.mock import patch
        from django.core.cache import cache
        cache.clear()
        stats = dict(self.stats, month="September 2026",
                     conversations={"total": 200, "prev_total": 100, "delta_pct": 100.0})
        fake_core = {"headline": "h", "assessment": "real_increase", "drivers": [
            {"type": "measurement_artifact", "description": "invented cause",
             "citations": [{"kind": "changelog", "date": "2020-01-01", "category": "incident"}]},
        ]}
        with patch.object(cc, "_build_stats", return_value=stats), \
             patch.object(cc, "_changelog_for_window", return_value=[self.change]), \
             patch.object(cc, "_generate_cost_commentary", return_value=fake_core):
            from datetime import date
            result = cc.cost_commentary_for(date(2026, 9, 1))
        d = result["drivers"][0]
        self.assertFalse(d["cited"])
        self.assertEqual(d["type"], "unexplained")
        self.assertEqual(result["claims_cited"], 0)
        cache.clear()


class QualityThemeTests(TestCase):
    """ENG-199 C1b: cited quality themes from flagged/low-scored chats."""

    def test_transcript_carries_quality_attributes(self):
        from .insights_views import _build_transcript
        from .models import Chat
        chat = Chat(chat_id="c1", model="m", evaluation_score=42,
                    investigation_status="flagged")
        text, _ = _build_transcript(chat, [])
        self.assertIn('evaluation_score="42"', text)
        self.assertIn('flagged="true"', text)
        self.assertIn('id="c1"', text)

    def test_transcript_omits_absent_attributes(self):
        from .insights_views import _build_transcript
        from .models import Chat
        chat = Chat(chat_id="c2", model="m", evaluation_score=None,
                    investigation_status="unflagged")
        text, _ = _build_transcript(chat, [])
        self.assertNotIn("evaluation_score=", text)
        self.assertNotIn("flagged=", text)

    def test_sanitize_drops_invented_example_ids(self):
        from .insights_views import _sanitize_quality_theme_examples
        core = {"quality_themes": [
            {"name": "T1", "summary": "s",
             "examples": ["real-1", "invented-9", "real-2"]},
            {"name": "T2", "summary": "s", "examples": ["invented-9"]},
            "not-a-dict",
        ]}
        out = _sanitize_quality_theme_examples(core, {"real-1", "real-2"})
        self.assertEqual(len(out["quality_themes"]), 1)
        self.assertEqual(out["quality_themes"][0]["examples"], ["real-1", "real-2"])

    def test_prompt_asks_for_quality_themes(self):
        from .insights_views import _build_prompt, LOW_SCORE_THRESHOLD
        prompt = _build_prompt(['<conversation id="a" evaluation_score="40">\nHi\n</conversation>'],
                               "September 2026", 1)
        self.assertIn("quality_themes", prompt)
        self.assertIn(str(LOW_SCORE_THRESHOLD), prompt)
        self.assertIn('flagged="true"', prompt)

    def test_quality_themes_in_list_fields(self):
        from .insights_views import _LIST_FIELDS, _sanitize_report, REPORT_INSIGHTS_TOOL
        self.assertIn("quality_themes", _LIST_FIELDS)
        self.assertIn("quality_themes", REPORT_INSIGHTS_TOOL["input_schema"]["required"])
        # sanitize keeps the field and drops malformed shapes
        out = _sanitize_report({"quality_themes": "nope", "headline": "h",
                                "top_requests": [], "unmet_needs": [],
                                "product_demand": [], "recommendations": []})
        self.assertEqual(out["quality_themes"], [])


class GraderPromptTests(TestCase):
    """ENG-199 C1c: the chat grader must not penalize dialect, slang,
    typos, code-switching, or non-standard grammar."""

    def test_prompt_has_dialect_robustness_wording(self):
        from . import views
        from .models import Chat, Message
        from unittest.mock import patch, MagicMock
        chat = Chat.objects.create(chat_id="g1", model="m", timestamp=timezone.now())
        Message.objects.create(chat=chat, content="u want dis card??",
                               llm_formatted_message="u want dis card??",
                               returned_content="Yes, we have it.",
                               llm_formatted_returned_message="Yes, we have it.",
                               tokens_in=10, tokens_out=10,
                               timestamp=timezone.now())
        captured = {}

        def fake_create(**kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            m = MagicMock()
            m.content = [MagicMock(text="100")]
            return m

        with patch("anthropic.Anthropic") as mock_client:
            mock_client.return_value.messages.create.side_effect = fake_create
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
                score = views.score_single_chat(chat)
        self.assertEqual(score, 100)
        prompt = captured["prompt"].lower()
        for phrase in ["dialect", "slang", "typos", "never lower the score",
                       "inferred intent", "writing style"]:
            self.assertIn(phrase, prompt)


class VerdictCardsEndpointTests(TestCase):
    """C3 deterministic verdict cards: computed thresholds over the same
    single-month computations the stats endpoints serve -- never
    LLM-generated. Candidates with insufficient data are omitted; an empty
    list with all_clear=True reproduces the dashboard's all-clear state."""

    RATES = {"claude-haiku-4-5": {"input": 0.000001, "output": 0.000005}}

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")
        self.this_month = _now().replace(day=1)
        self.prev_month = (self.this_month.replace(day=1) - timedelta(days=1)).replace(day=1)

    def tearDown(self):
        cache.clear()

    def _patch_adapter(self, spend=0.0, rates=None, usage=None):
        get_cost = MagicMock(return_value=_cost_resp(spend))
        get_model_rates = MagicMock(return_value={"rates": rates if rates is not None else {}})
        get_usage_by_key = MagicMock(return_value=usage if usage is not None else {"keys": []})
        p = patch.multiple("cost_management.views.llmprovider",
                           get_cost=get_cost, get_model_rates=get_model_rates,
                           get_usage_by_key=get_usage_by_key)
        p.start()
        self.addCleanup(p.stop)
        return get_cost, get_model_rates, get_usage_by_key

    def _chat(self, chat_id, month, tokens_in=0, tokens_out=0,
              likely_automated=False, score=None, model="claude-haiku-4-5"):
        chat = Chat.objects.create(
            chat_id=chat_id, model=model, tokens_in=tokens_in, tokens_out=tokens_out,
            likely_automated=likely_automated, evaluation_score=score,
        )
        Chat.objects.filter(pk=chat.pk).update(timestamp=month.replace(day=10))
        return chat

    def _hurting_usage(self):
        return {"keys": [{
            "api_key_id": "apikey_chat", "name": "chat",
            "input_tokens": 1_001_000, "output_tokens": 0,
            "by_model": {"m": {
                "uncached_input_tokens": 0, "output_tokens": 0,
                "cache_creation_tokens": 1_000_000, "cache_read_tokens": 1_000,
            }},
        }]}

    def _hurting_rates(self):
        return {"m": {
            "input": 0.000001, "cache_creation": 0.000002, "cache_read": 0.0000005,
        }}

    def _ids(self, payload):
        return [v["id"] for v in payload["verdicts"]]

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/cost/verdicts/").status_code, 401)

    def test_invalid_month_is_400(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/cost/verdicts/?month=nope").status_code, 400)

    def test_all_clear_when_nothing_triggers(self):
        # billed == logged (no recon gap), no cache data, one scored chat
        # (below the sample minimum), no bot traffic.
        self._patch_adapter(spend=3.5, rates=self.RATES)
        self._chat("c-1", self.this_month, 1_000_000, 500_000, score=80)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/verdicts/").json()

        self.assertEqual(d["month"], self.this_month.strftime("%Y-%m"))
        self.assertEqual(d["verdicts"], [])
        self.assertTrue(d["all_clear"])

    def test_cache_hurting_card(self):
        self._patch_adapter(spend=0.0, rates=self._hurting_rates(),
                            usage=self._hurting_usage())
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/verdicts/").json()

        self.assertEqual(self._ids(d), ["cache-hurting"])
        self.assertFalse(d["all_clear"])
        card = d["verdicts"][0]
        self.assertEqual(card["kind"], "cache")
        self.assertEqual(card["tone"], "bad")
        self.assertIn("caching", card["headline"].lower())
        self.assertIn("primary", card)
        self.assertTrue(card["alternatives"])
        self.assertEqual(card["deep_link"], "panel-cache-economics")
        for ev in card["evidence"]:
            self.assertEqual(set(ev.keys()), {"kind", "metric", "value", "source"})
            self.assertEqual(ev["kind"], "stat")
            self.assertEqual(ev["source"], "cache_economics")
        metrics = {ev["metric"] for ev in card["evidence"]}
        self.assertIn("cache.savings", metrics)

    def test_cache_no_data_is_omitted(self):
        # No usage rows and no rates -> cache verdict "no_data" -> skip.
        self._patch_adapter(spend=0.0)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/verdicts/").json()

        self.assertEqual(d["verdicts"], [])
        self.assertTrue(d["all_clear"])

    def test_recon_unaccounted_card(self):
        # billed 15.0, logged 3.5 -> 76.7% unaccounted, over the 5% bar.
        self._patch_adapter(spend=15.0, rates=self.RATES)
        self._chat("c-1", self.this_month, 1_000_000, 500_000)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/verdicts/").json()

        self.assertEqual(self._ids(d), ["recon-unaccounted"])
        card = d["verdicts"][0]
        self.assertEqual(card["tone"], "bad")
        self.assertIn("76.7%", card["headline"])
        self.assertEqual(card["deep_link"], "panel-cost-reconciliation")
        self.assertIn("primary", card)
        metrics = {ev["metric"]: ev["value"] for ev in card["evidence"]}
        self.assertEqual(metrics["recon.billed_spend"], 15.0)
        self.assertEqual(metrics["recon.unaccounted"], round(15.0 - 3.5, 2))
        self.assertEqual(metrics["recon.unaccounted_pct"], round(11.5 / 15.0 * 100, 1))

    def test_recon_below_threshold_is_omitted(self):
        # billed == logged -> 0% unaccounted, nothing to say.
        self._patch_adapter(spend=3.5, rates=self.RATES)
        self._chat("c-1", self.this_month, 1_000_000, 500_000)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/verdicts/").json()

        self.assertNotIn("recon-unaccounted", self._ids(d))

    def test_recon_skipped_when_billed_unavailable(self):
        # Cost source down -> billed_spend None -> skip, don't invent.
        get_cost = MagicMock(return_value={"error": "boom"})
        get_model_rates = MagicMock(return_value={"rates": self.RATES})
        get_usage_by_key = MagicMock(return_value={"keys": []})
        p = patch.multiple("cost_management.views.llmprovider",
                           get_cost=get_cost, get_model_rates=get_model_rates,
                           get_usage_by_key=get_usage_by_key)
        p.start()
        self.addCleanup(p.stop)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/verdicts/").json()

        self.assertNotIn("recon-unaccounted", self._ids(d))
        self.assertTrue(d["all_clear"])

    def test_eval_drop_card(self):
        self._patch_adapter(spend=0.0)
        for i in range(12):
            self._chat(f"now-{i}", self.this_month, score=70)
            self._chat(f"prev-{i}", self.prev_month, score=80)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/verdicts/").json()

        self.assertEqual(self._ids(d), ["eval-drop"])
        card = d["verdicts"][0]
        self.assertEqual(card["tone"], "bad")
        self.assertIn("10 points", card["headline"])
        self.assertEqual(card["deep_link"], "panel-quality-themes")
        metrics = {ev["metric"]: ev["value"] for ev in card["evidence"]}
        self.assertEqual(metrics["eval.avg"], 70.0)
        self.assertEqual(metrics["eval.prev_avg"], 80.0)
        self.assertEqual(metrics["eval.scored"], 12)

    def test_eval_drop_skipped_below_sample_minimum(self):
        self._patch_adapter(spend=0.0)
        for i in range(3):
            self._chat(f"now-{i}", self.this_month, score=60)
            self._chat(f"prev-{i}", self.prev_month, score=80)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/verdicts/").json()

        self.assertNotIn("eval-drop", self._ids(d))

    def test_eval_no_drop_no_card(self):
        self._patch_adapter(spend=0.0)
        for i in range(12):
            self._chat(f"now-{i}", self.this_month, score=79)
            self._chat(f"prev-{i}", self.prev_month, score=80)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/verdicts/").json()

        self.assertNotIn("eval-drop", self._ids(d))
        self.assertTrue(d["all_clear"])

    def test_bot_share_drift_card(self):
        # This month: bot 20.0 / real 1.0 -> 95.2%. Prev: bot 1.0 / real 20.0 -> 4.8%.
        # Billed == logged so reconciliation stays quiet.
        self._patch_adapter(spend=21.0, rates=self.RATES)
        self._chat("bot-now", self.this_month, tokens_in=20_000_000, likely_automated=True)
        self._chat("real-now", self.this_month, tokens_in=1_000_000)
        self._chat("bot-prev", self.prev_month, tokens_in=1_000_000, likely_automated=True)
        self._chat("real-prev", self.prev_month, tokens_in=20_000_000)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/verdicts/").json()

        self.assertEqual(self._ids(d), ["bot-share-drift"])
        card = d["verdicts"][0]
        self.assertEqual(card["kind"], "bot-share")
        self.assertEqual(card["tone"], "flat")
        self.assertIn("95%", card["headline"])
        self.assertIsNone(card["deep_link"])
        metrics = {ev["metric"]: ev["value"] for ev in card["evidence"]}
        self.assertEqual(metrics["bot_share.pct"], 95.2)
        self.assertEqual(metrics["bot_share.prev_pct"], 4.8)

    def test_bot_share_skipped_without_rates(self):
        # No rates -> share is None, not zero -> skip, don't claim it's clean.
        self._patch_adapter(spend=21.0, rates={})
        self._chat("bot-now", self.this_month, tokens_in=20_000_000, likely_automated=True)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/verdicts/").json()

        self.assertNotIn("bot-share-drift", self._ids(d))

    def test_caches_and_refresh_bypasses(self):
        get_cost, _, _ = self._patch_adapter(spend=0.0)
        self.client.force_login(self.user)

        self.client.get("/api/cost/verdicts/")
        calls_after_first = get_cost.call_count
        cached = self.client.get("/api/cost/verdicts/").json()
        self.assertTrue(cached["cached"])
        self.assertEqual(get_cost.call_count, calls_after_first)

        self.client.get("/api/cost/verdicts/?refresh=1")
        self.assertGreater(get_cost.call_count, calls_after_first)


class AlertRulesTests(TestCase):
    """C4 alerts + preference memory: DB-backed rules, fire-once-per-breach
    semantics, cooldowns, Slack payload shape, and the settings API."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")
        # The 0020 data migration seeds 4 default rules; each test below
        # starts from a clean slate (seed coverage has its own test).
        AlertRule.objects.all().delete()

    def tearDown(self):
        cache.clear()

    def test_seed_migration_creates_conservative_defaults(self):
        import importlib

        migration = importlib.import_module(
            "cost_management.migrations.0020_seed_default_alert_rules"
        )
        from django.apps import apps as django_apps

        migration.seed_rules(django_apps, None)
        rules = {r.rule_type: r for r in AlertRule.objects.all()}
        self.assertEqual(
            sorted(rules),
            ["cache_hit_rate_drop", "cost_per_conversation", "eval_score_drop", "spend_anomaly"],
        )
        self.assertTrue(all(r.enabled for r in rules.values()))
        # Conservative bars: above what the C3 verdicts flag.
        self.assertGreaterEqual(rules["eval_score_drop"].threshold, 5.0)
        self.assertGreaterEqual(rules["cost_per_conversation"].threshold, 0.10)

    def _mkrule(self, **kw):
        params = {
            "rule_type": "cost_per_conversation",
            "name": "Cost check",
            "threshold": 0.10,
            "cooldown_hours": 24,
            "enabled": True,
        }
        params.update(kw)
        return AlertRule.objects.create(**params)

    def _breached_metrics(self, value=0.25):
        """Patch every metric function to report a breach at `value`."""
        ev = [{"kind": "stat", "metric": "cost.per_conversation",
               "value": value, "source": "monthly_stats"}]
        p = patch.multiple(
            "cost_management.alerts",
            _metric_cost_per_conversation=MagicMock(return_value=(True, value, None, ev)),
            _metric_spend_anomaly=MagicMock(return_value=(True, value, None, ev)),
            _metric_cache_drop=MagicMock(return_value=(True, value, None, ev)),
            _metric_eval_drop=MagicMock(return_value=(True, value, "headline", ev)),
        )
        p.start()
        self.addCleanup(p.stop)

    def _recovered_metrics(self):
        p = patch.multiple(
            "cost_management.alerts",
            _metric_cost_per_conversation=MagicMock(return_value=(False, 0.01, None, [])),
            _metric_spend_anomaly=MagicMock(return_value=(False, 0.01, None, [])),
            _metric_cache_drop=MagicMock(return_value=(False, 0.01, None, [])),
            _metric_eval_drop=MagicMock(return_value=(False, 0.01, None, [])),
        )
        p.start()
        self.addCleanup(p.stop)

    # -- auth -----------------------------------------------------------
    def test_requires_login(self):
        for method, url in [
            ("get", "/api/cost/alert_rules/"),
            ("post", "/api/cost/alert_rules/"),
            ("put", "/api/cost/alert_rules/1/"),
            ("delete", "/api/cost/alert_rules/1/"),
            ("post", "/api/cost/alert_firings/1/acknowledge/"),
            ("get", "/api/cost/preferences/"),
        ]:
            resp = getattr(self.client, method)(url)
            self.assertEqual(resp.status_code, 401, f"{method} {url}")

    # -- settings API CRUD ----------------------------------------------
    def test_create_rule(self):
        self.client.force_login(self.user)
        resp = self.client.post(
            "/api/cost/alert_rules/",
            data=json.dumps({"rule_type": "spend_anomaly", "name": "Spend watch",
                             "threshold": 2.0, "cooldown_hours": 48}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()["rule"]
        self.assertEqual(body["rule_type"], "spend_anomaly")
        self.assertEqual(body["threshold"], 2.0)
        self.assertEqual(body["cooldown_hours"], 48)
        self.assertTrue(body["enabled"])
        self.assertFalse(body["breached"])
        self.assertIn("unit", body)

    def test_create_rule_defaults_name(self):
        self.client.force_login(self.user)
        resp = self.client.post(
            "/api/cost/alert_rules/",
            data=json.dumps({"rule_type": "eval_score_drop", "threshold": 8.0}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()["rule"]["name"])

    def test_create_rule_rejects_bad_input(self):
        self.client.force_login(self.user)
        for payload in [
            {"rule_type": "nope", "threshold": 1.0},
            {"rule_type": "spend_anomaly", "threshold": -2.0},
            {"rule_type": "spend_anomaly", "threshold": "x"},
            {"rule_type": "spend_anomaly", "threshold": 2.0, "cooldown_hours": 0},
        ]:
            resp = self.client.post(
                "/api/cost/alert_rules/", data=json.dumps(payload),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 400, payload)

    def test_update_rule_threshold_without_deploy(self):
        self.client.force_login(self.user)
        rule = self._mkrule(threshold=0.10)
        resp = self.client.put(
            f"/api/cost/alert_rules/{rule.id}/",
            data=json.dumps({"threshold": 0.50, "cooldown_hours": 72,
                             "enabled": False, "name": "Renamed"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        rule.refresh_from_db()
        self.assertEqual(rule.threshold, 0.50)
        self.assertEqual(rule.cooldown_hours, 72)
        self.assertFalse(rule.enabled)
        self.assertEqual(rule.name, "Renamed")

    def test_update_rule_type_is_immutable(self):
        self.client.force_login(self.user)
        rule = self._mkrule()
        resp = self.client.put(
            f"/api/cost/alert_rules/{rule.id}/",
            data=json.dumps({"rule_type": "spend_anomaly", "threshold": 0.10}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        rule.refresh_from_db()
        self.assertEqual(rule.rule_type, "cost_per_conversation")

    def test_delete_rule(self):
        self.client.force_login(self.user)
        rule = self._mkrule()
        resp = self.client.delete(f"/api/cost/alert_rules/{rule.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AlertRule.objects.filter(id=rule.id).exists())

    def test_list_rules_includes_state(self):
        self.client.force_login(self.user)
        self._mkrule()
        body = self.client.get("/api/cost/alert_rules/").json()
        self.assertEqual(len(body["rules"]), 1)
        self.assertIn("rule_types", body)
        self.assertIn("recent_firings", body)

    # -- firing semantics ------------------------------------------------
    def test_breach_fires_exactly_once_with_evidence(self):
        from cost_management.alerts import evaluate_all_rules

        self._mkrule(threshold=0.10)
        self._breached_metrics(value=0.25)
        summary = evaluate_all_rules()
        self.assertEqual(summary["fired"], 1)
        firings = AlertFiring.objects.all()
        self.assertEqual(firings.count(), 1)
        firing = firings[0]
        self.assertEqual(firing.metric_value, 0.25)
        self.assertTrue(firing.headline)
        # C1-idiom evidence citations travel with the firing.
        self.assertEqual(firing.evidence[0]["metric"], "cost.per_conversation")
        self.assertTrue(AlertRule.objects.get().breached)

    def test_no_refire_while_still_breached(self):
        from cost_management.alerts import evaluate_all_rules

        self._mkrule(threshold=0.10)
        self._breached_metrics(value=0.25)
        evaluate_all_rules()
        summary = evaluate_all_rules()
        self.assertEqual(summary["fired"], 0)
        self.assertEqual(AlertFiring.objects.count(), 1)

    def test_rebreach_within_cooldown_suppressed(self):
        from cost_management.alerts import evaluate_all_rules

        rule = self._mkrule(threshold=0.10, cooldown_hours=24)
        self._breached_metrics(value=0.25)
        evaluate_all_rules()
        # Breach clears, then restarts inside the cooldown window.
        self._recovered_metrics()
        evaluate_all_rules()
        rule.refresh_from_db()
        self.assertFalse(rule.breached)
        self._breached_metrics(value=0.30)
        summary = evaluate_all_rules()
        self.assertEqual(summary["fired"], 0)
        self.assertEqual(AlertFiring.objects.count(), 1)

    def test_rebreach_after_cooldown_fires(self):
        from cost_management.alerts import evaluate_all_rules

        rule = self._mkrule(threshold=0.10, cooldown_hours=24)
        self._breached_metrics(value=0.25)
        evaluate_all_rules()
        self._recovered_metrics()
        evaluate_all_rules()
        # Age the firing past the cooldown, then re-breach.
        AlertFiring.objects.update(
            fired_at=timezone.now() - timezone.timedelta(hours=25)
        )
        self._breached_metrics(value=0.30)
        summary = evaluate_all_rules()
        self.assertEqual(summary["fired"], 1)
        self.assertEqual(AlertFiring.objects.count(), 2)

    def test_disabled_rule_never_fires(self):
        from cost_management.alerts import evaluate_all_rules

        self._mkrule(threshold=0.10, enabled=False)
        self._breached_metrics(value=0.25)
        summary = evaluate_all_rules()
        self.assertEqual(summary["fired"], 0)
        self.assertEqual(AlertFiring.objects.count(), 0)

    def test_month_rollover_resets_breach_flags(self):
        from cost_management.alerts import _maybe_reset_month

        rule = self._mkrule()
        rule.breached = True
        rule.save(update_fields=["breached"])
        OperatorPreference.objects.create(
            key="alerts_last_eval_month", value={"month": "2020-01"}
        )
        _maybe_reset_month(timezone.now().date().replace(day=1))
        rule.refresh_from_db()
        self.assertFalse(rule.breached)

    # -- C3 evaluator reuse ----------------------------------------------
    def test_eval_verdict_threshold_override(self):
        from cost_management.verdicts import _eval_verdict

        ctx = {"eval_avg": 92.0, "prev_eval_avg": 95.0, "scored": 20}
        self.assertIsNone(_eval_verdict(ctx))  # 3pt drop < default 5
        card = _eval_verdict(ctx, drop_points=2.0)
        self.assertIsNotNone(card)
        self.assertEqual(card["id"], "eval-drop")
        self.assertTrue(card["evidence"])

    # -- Slack delivery ----------------------------------------------------
    def test_slack_post_payload_shape(self):
        from cost_management import alerts as alerts_mod

        with patch.dict(os.environ, {"SLACK_ALERTS_WEBHOOK_URL": "https://hooks.slack.test/x"}):
            with patch("urllib.request.urlopen") as mock_open:
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_open.return_value.__enter__.return_value = mock_resp
                ok = alerts_mod._post_slack(
                    "Cost per conversation hit $0.25",
                    ["Rule: Cost check", "• `cost.per_conversation` = `0.25`"],
                    "panel-cost-commentary",
                )
        self.assertTrue(ok)
        req = mock_open.call_args[0][0]
        payload = json.loads(req.data.decode())
        self.assertIn("Cost per conversation hit $0.25", payload["text"])
        self.assertIn("panel-cost-commentary", payload["text"])
        self.assertIn("cost.per_conversation", payload["text"])

    def test_slack_unset_webhook_fails_silent(self):
        from cost_management import alerts as alerts_mod

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SLACK_ALERTS_WEBHOOK_URL", None)
            with patch("urllib.request.urlopen") as mock_open:
                ok = alerts_mod._post_slack("h", [], None)
        self.assertFalse(ok)
        mock_open.assert_not_called()

    def test_slack_transport_error_fails_silent(self):
        from cost_management import alerts as alerts_mod

        with patch.dict(os.environ, {"SLACK_ALERTS_WEBHOOK_URL": "https://hooks.slack.test/x"}):
            with patch("urllib.request.urlopen", side_effect=Exception("boom")):
                ok = alerts_mod._post_slack("h", [], None)
        self.assertFalse(ok)

    # -- acknowledge -------------------------------------------------------
    def test_acknowledge_firing(self):
        self.client.force_login(self.user)
        rule = self._mkrule()
        firing = AlertFiring.objects.create(
            rule=rule, metric_value=0.25, headline="breach", evidence=[]
        )
        resp = self.client.post(f"/api/cost/alert_firings/{firing.id}/acknowledge/")
        self.assertEqual(resp.status_code, 200)
        firing.refresh_from_db()
        self.assertIsNotNone(firing.acknowledged_at)
        # Idempotent: acknowledging twice keeps the first timestamp.
        first = firing.acknowledged_at
        resp = self.client.post(f"/api/cost/alert_firings/{firing.id}/acknowledge/")
        self.assertEqual(resp.status_code, 200)
        firing.refresh_from_db()
        self.assertEqual(firing.acknowledged_at, first)

    # -- preferences ---------------------------------------------------------
    def test_preferences_crud(self):
        self.client.force_login(self.user)
        # Empty to start.
        self.assertEqual(
            self.client.get("/api/cost/preferences/").json(), {"preferences": {}}
        )
        # Upsert.
        resp = self.client.put(
            "/api/cost/preferences/",
            data=json.dumps({"key": "dashboard_month", "value": "2026-09"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = self.client.get("/api/cost/preferences/").json()
        self.assertEqual(body["preferences"]["dashboard_month"], "2026-09")
        # Delete.
        resp = self.client.delete("/api/cost/preferences/dashboard_month/")
        self.assertEqual(resp.status_code, 200)
        body = self.client.get("/api/cost/preferences/").json()
        self.assertEqual(body, {"preferences": {}})

    def test_preferences_reject_bad_input(self):
        self.client.force_login(self.user)
        resp = self.client.put(
            "/api/cost/preferences/",
            data=json.dumps({"value": "x"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
