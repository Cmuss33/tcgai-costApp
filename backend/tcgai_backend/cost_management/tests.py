import json
import os
from unittest.mock import patch, MagicMock

import requests

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings

from .models import Chat, Message
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


class InsightsSummaryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")

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
        self.assertEqual(response.status_code, 302)

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
        get_cost = MagicMock(side_effect=lambda year, month:
                             _cost_resp(5.0, 7.0, 3.0) if month == cur else _cost_resp(4.0, 6.0))
        get_tokens = MagicMock(side_effect=lambda year, month:
                               _tok_resp((1000, 300), (2000, 400)) if month == cur else _tok_resp((800, 200)))
        p = patch.multiple("cost_management.views.llmprovider",
                           get_cost=get_cost, get_tokens=get_tokens)
        p.start()
        self.addCleanup(p.stop)
        return get_cost, get_tokens

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/cost/monthly_stats/").status_code, 302)

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

    def test_likely_automated_chats_are_excluded_from_every_stat(self):
        """ENG-149/150: a flagged bot chat must not appear in the dashboard's
        conversation count, daily breakdown, busiest-day, model mix, or the
        per-conversation cost denominator -- flag_automated_chats sets this
        field on exactly the chats that corrupted these numbers in prod."""
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
        self.assertEqual(d["per_conversation"]["cost"], round(15.0 / 3, 4))
        self.assertEqual(sum(m["conversations"] for m in d["model_mix"]), 3)

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
        p = patch.multiple("cost_management.views.llmprovider", get_cost=get_cost, get_tokens=get_tokens)
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
        p = patch.multiple("cost_management.views.llmprovider", get_cost=get_cost, get_tokens=get_tokens)
        p.start()
        self.addCleanup(p.stop)
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/monthly_stats/").json()

        self.assertIsNone(d["tokens"]["cache_hit_rate"])
        self.assertEqual(d["tokens"]["cache_creation"], 0)
        self.assertEqual(d["tokens"]["cache_read"], 0)


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
        self.assertEqual(by_id["apikey_chat"]["by_model"]["claude-sonnet-5"]["input_tokens"], 500)
        self.assertEqual(by_id["apikey_search"]["name"], "prod-shopify-search")
        self.assertEqual(by_id["apikey_search"]["input_tokens"], 300)

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
        self.assertEqual(self.client.get("/api/cost/get_usage_by_key/").status_code, 302)

    def test_combines_tokens_and_rates_into_estimated_cost(self):
        self._patch_adapter(
            usage_return={"keys": [{
                "api_key_id": "apikey_chat", "name": "prod-shopify-chatbot",
                "input_tokens": 1_000_000, "output_tokens": 500_000,
                "by_model": {"claude-haiku-4-5": {"input_tokens": 1_000_000, "output_tokens": 500_000}},
            }], "workspace_id": "wrkspc_target"},
            rates_return={"rates": {"claude-haiku-4-5": {"input": 0.000001, "output": 0.000005}}},
        )
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/get_usage_by_key/").json()

        self.assertEqual(d["keys"][0]["estimated_cost"], 1.0 + 2.5)
        self.assertTrue(d["estimated"])

    def test_missing_rate_for_a_model_contributes_zero_not_an_error(self):
        self._patch_adapter(
            usage_return={"keys": [{
                "api_key_id": "apikey_x", "name": "x",
                "input_tokens": 100, "output_tokens": 100,
                "by_model": {"some-unpriced-model": {"input_tokens": 100, "output_tokens": 100}},
            }], "workspace_id": None},
            rates_return={"rates": {}},
        )
        self.client.force_login(self.user)

        d = self.client.get("/api/cost/get_usage_by_key/").json()

        self.assertEqual(d["keys"][0]["estimated_cost"], 0.0)

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
                 "by_model": {"m": {"input_tokens": 10, "output_tokens": 0}}},
                {"api_key_id": "big", "name": "big", "input_tokens": 1000, "output_tokens": 0,
                 "by_model": {"m": {"input_tokens": 1000, "output_tokens": 0}}},
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
        self.assertEqual(self.client.get("/api/cost/get_model_rates/").status_code, 302)

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
