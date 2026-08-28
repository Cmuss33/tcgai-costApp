import json
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from .models import Chat, Message
from .issue_trackers import GitHubIssueTracker, IssueRef, IssueTrackerError


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
