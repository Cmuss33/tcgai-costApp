import json

from django.contrib.auth.models import User
from django.test import TestCase

from .models import Chat, Message


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
