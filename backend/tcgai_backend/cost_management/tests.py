import json
from unittest.mock import patch, MagicMock

import requests

from django.contrib.auth.models import User
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
