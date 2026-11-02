"""Judge grounding: the deterministic evidence block + score caps.

The evaluator used to grade the transcript text alone. Chat c5b596ef
(2026-11-01) scored 92 from all five judge samples while containing five
self-retractions of invented products, one provably false retraction
(in-stock sleeves retracted as "not in our catalog results"), and an
Abuse Guard closure -- none of which the judge could weigh, because it
never saw the store's own records. These tests pin the deterministic
parts: evidence assembly from stored rows, and the caps applied in code
after the judge scores. The judge itself is mocked throughout -- no test
here fakes an LLM verdict as a result.
"""
import os
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from django.test import TestCase

from .eval_evidence import build_evidence, apply_score_caps, render_evidence_block
from .models import Chat, Message


def msg(user, assistant, products_shown=None, raw_returned=""):
    return SimpleNamespace(
        content=user,
        returned_content=assistant,
        products_shown=products_shown,
        llm_formatted_returned_message=raw_returned,
    )


DECKS_SHOWN = {
    "primary": [
        {"id": "gid://shopify/Product/9130066149550",
         "title": "Pokémon TCG: Mega Greninja ex Premium Collection",
         "price": "69.99", "available": True},
        {"id": "gid://shopify/Product/9130056777902",
         "title": "Pokémon TCG: Mega Lucario ex League Battle Deck",
         "price": "24.99", "available": True},
    ],
    "complementary": [],
}

SLEEVES_SHOWN = {
    "primary": [
        {"id": "gid://shopify/Product/8219892351150",
         "title": "Dragon Shield Matte Dual Sleeves - Truth",
         "price": "12.75", "available": True},
        {"id": "gid://shopify/Product/9017748324526",
         "title": "Dragon Shield 25th Special Edition - Matte Dual Sleeves - Cobalt Silver",
         "price": "13.99", "available": True},
        {"id": "gid://shopify/Product/9000000000000",
         "title": "50+1 Mystery Pack / ETB Card Sleeves",
         "price": "5.00", "available": True},
    ],
    "complementary": [],
}

CLOSURE_TEXT = (
    "This chat is for product, stock, price, event, and store questions, "
    "so I can't continue this conversation. If you need other help, "
    "please contact the store directly."
)


def c5b596ef_messages():
    """The stored shape of chat c5b596ef, compressed to the turns that
    carry the evidence: an invention, its legitimate correction, the
    sleeves actually shown, the false retraction, and the closure."""
    return [
        msg("Do you have the League Battle Deck Mega Greninja EX",
            "No exact Mega Greninja League Battle Deck, but here are the closest matches.",
            products_shown=DECKS_SHOWN),
        msg("When will you have it",
            "The Mega Charizard X ex is CAD 54.99, and our Store Tournament Season 6 event is coming up.",
            products_shown=DECKS_SHOWN),
        msg("When will you have it",
            "I don't have a date for it. I also need to correct my last message: "
            "the Mega Charizard X ex price and the Store Tournament Season 6 event "
            "weren't from our search results, so please disregard them.",
            products_shown=DECKS_SHOWN),
        msg("What about greninja sleeves",
            "Here are the sleeves we have in stock. Want a specific color or brand?",
            products_shown=SLEEVES_SHOWN),
        msg("Noooooo",
            "I'm sorry about that. I need to correct my last message: I shouldn't have "
            "listed those Dragon Shield sleeves or the Delta Reign pre-order. Neither "
            "showed up in our catalog results, and I can't confirm sleeves are in stock right now."),
        msg("Sleeves that are greninja dumbass", CLOSURE_TEXT),
    ]


class EvidenceAssemblyTests(TestCase):
    def test_products_union_and_per_turn(self):
        ev = build_evidence(c5b596ef_messages())
        self.assertIn("Dragon Shield Matte Dual Sleeves - Truth",
                      ev["products_shown_titles"])
        self.assertIn("Pokémon TCG: Mega Greninja ex Premium Collection",
                      ev["products_shown_titles"])
        self.assertEqual(ev["products_shown_total"], 5)
        turn4 = [t for t in ev["turns"] if t["turn"] == 4][0]
        self.assertEqual(len(turn4["products_shown"]), 3)

    def test_retractions_detected_with_turns(self):
        ev = build_evidence(c5b596ef_messages())
        turns = [r["turn"] for r in ev["retractions"]]
        self.assertEqual(turns, [3, 5])

    def test_legit_correction_is_not_false(self):
        """Turn 3 retracts the Charizard price/event -- neither was ever
        in the shown-products evidence, so it is a legitimate correction
        and must not be flagged false."""
        ev = build_evidence(c5b596ef_messages())
        turn3 = [r for r in ev["retractions"] if r["turn"] == 3][0]
        self.assertFalse(turn3["false"])
        self.assertEqual(turn3["matched_shown_titles"], [])

    def test_false_retraction_of_shown_sleeves(self):
        """Turn 5 denies the Dragon Shield sleeves showed up in the
        results; they were shown on turn 4. That is the false retraction
        at the heart of c5b596ef."""
        ev = build_evidence(c5b596ef_messages())
        turn5 = [r for r in ev["retractions"] if r["turn"] == 5][0]
        self.assertTrue(turn5["false"])
        self.assertTrue(
            any("Dragon Shield" in t for t in turn5["matched_shown_titles"]))
        self.assertTrue(ev["false_retraction"])

    def test_guard_closure_end_state(self):
        ev = build_evidence(c5b596ef_messages())
        self.assertEqual(ev["end_state"], "guard_closure")

    def test_normal_ending_is_not_closure(self):
        ev = build_evidence([
            msg("hi", "Hello! How can I help?"),
            msg("do you have charizard", "Yes, here it is."),
        ])
        self.assertEqual(ev["end_state"], "normal")
        self.assertEqual(ev["retractions"], [])
        self.assertFalse(ev["false_retraction"])

    def test_price_correction_about_shown_product_is_not_false(self):
        """Correcting a stated price of a product that WAS shown is a
        retraction referencing evidence, but it denies nothing about the
        product's presence -- not a false retraction."""
        ev = build_evidence([
            msg("sleeves?", "Here are the sleeves we have in stock.",
                products_shown=SLEEVES_SHOWN),
            msg("how much were the truth ones",
                "I need to correct my last message: the Dragon Shield Matte Dual "
                "Sleeves - Truth are CAD 12.75, not CAD 15.00."),
        ])
        self.assertEqual(len(ev["retractions"]), 1)
        self.assertFalse(ev["retractions"][0]["false"])
        self.assertTrue(ev["retractions"][0]["matched_shown_titles"])

    def test_unanswered_turns_counted(self):
        ev = build_evidence([
            msg("q1", "answer"),
            msg("q2", "", raw_returned='{"stop_reason": "end_turn"}'),
            msg("q3", "", raw_returned='{"stop_reason": "tool_use"}'),
        ])
        self.assertEqual(ev["unanswered_turns"], 1)

    def test_evidence_block_is_factual_and_names_products(self):
        block = render_evidence_block(build_evidence(c5b596ef_messages()))
        self.assertIn("Dragon Shield Matte Dual Sleeves - Truth", block)
        self.assertIn("FALSE", block)
        self.assertIn("guard closed", block.lower())


class ScoreCapTests(TestCase):
    def test_closure_caps_at_59(self):
        ev = build_evidence([msg("q", CLOSURE_TEXT)])
        final, caps = apply_score_caps(92, ev)
        self.assertEqual(final, 59)
        self.assertEqual([c["rule"] for c in caps], ["guard_closure"])

    def test_false_retraction_caps_at_39(self):
        ev = build_evidence(c5b596ef_messages()[:5])  # stop before closure
        final, caps = apply_score_caps(92, ev)
        self.assertEqual(final, 39)
        self.assertIn("false_retraction", [c["rule"] for c in caps])

    def test_retraction_about_shown_product_caps_at_79(self):
        ev = build_evidence([
            msg("sleeves?", "Here are the sleeves we have in stock.",
                products_shown=SLEEVES_SHOWN),
            msg("how much were the truth ones",
                "I need to correct my last message: the Dragon Shield Matte Dual "
                "Sleeves - Truth are CAD 12.75, not CAD 15.00."),
        ])
        final, caps = apply_score_caps(95, ev)
        self.assertEqual(final, 79)
        self.assertEqual([c["rule"] for c in caps], ["retraction_of_shown_product"])

    def test_legit_correction_only_no_cap(self):
        """Retractions whose subjects were never in the shown evidence
        (the bot correcting its own inventions) do not trigger the caps;
        the judge's own score stands."""
        ev = build_evidence(c5b596ef_messages()[:3])
        final, caps = apply_score_caps(95, ev)
        self.assertEqual(final, 95)
        self.assertEqual(caps, [])

    def test_caps_stack_by_minimum(self):
        ev = build_evidence(c5b596ef_messages())
        final, caps = apply_score_caps(92, ev)
        self.assertEqual(final, 39)
        self.assertEqual({c["rule"] for c in caps},
                         {"guard_closure", "false_retraction"})

    def test_cap_never_raises_a_low_score(self):
        ev = build_evidence(c5b596ef_messages())
        final, _ = apply_score_caps(20, ev)
        self.assertEqual(final, 20)


class GroundedScoringIntegrationTests(TestCase):
    """score_single_chat with the judge mocked: the deterministic
    pipeline (evidence into the prompt, caps onto the stored score,
    receipts onto the row) is what's under test."""

    @staticmethod
    def _judge_resp(text):
        resp = MagicMock()
        resp.content = [MagicMock(text=text)]
        return resp

    def _create_chat(self, chat_id, rows):
        chat = Chat.objects.create(chat_id=chat_id, model="claude-haiku-4-5")
        for user, assistant, shown in rows:
            Message.objects.create(
                chat=chat, content=user, returned_content=assistant,
                products_shown=shown,
                tokens_in=10, tokens_out=10, model="claude-haiku-4-5",
            )
        return chat

    @patch("anthropic.Anthropic")
    def test_c5b596ef_shape_cannot_score_in_the_90s(self, mock_anthropic_class):
        from .views import score_single_chat
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = self._judge_resp(
            "The assistant correctly understood the shopper's inferred intent and "
            "gave an honest, helpful answer.\nSCORE: 92")

        fixture = c5b596ef_messages()
        rows = [(m.content, m.returned_content, m.products_shown) for m in fixture]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            chat = self._create_chat("c5b596ef-shape", rows)
            stored = score_single_chat(chat)

        # The judge said 92 five times; the records say false retraction
        # + guard closure. The stored score is the capped one.
        self.assertEqual(stored, 39)
        chat.refresh_from_db()
        self.assertEqual(chat.evaluation_score, 39)
        self.assertEqual(chat.evaluation_samples, [92, 92, 92, 92, 92])
        ev = chat.evaluation_evidence
        self.assertIsNotNone(ev)
        self.assertEqual(ev["judge_score"], 92)
        self.assertTrue(ev["false_retraction"])
        self.assertEqual(ev["end_state"], "guard_closure")
        self.assertEqual({c["rule"] for c in ev["caps_applied"]},
                         {"guard_closure", "false_retraction"})
        self.assertEqual(chat.evaluation_rubric_version, "v4")

    @patch("anthropic.Anthropic")
    def test_judge_prompt_carries_the_evidence(self, mock_anthropic_class):
        from .views import score_single_chat
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = self._judge_resp("Fine.\nSCORE: 92")

        fixture = c5b596ef_messages()
        rows = [(m.content, m.returned_content, m.products_shown) for m in fixture]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            chat = self._create_chat("prompt-evidence", rows)
            score_single_chat(chat)

        prompt = mock_client.messages.create.call_args[1]["messages"][0]["content"]
        self.assertIn("EVIDENCE FROM THE STORE'S OWN RECORDS", prompt)
        self.assertIn("Dragon Shield Matte Dual Sleeves - Truth", prompt)
        self.assertIn("guard closed", prompt.lower())

    @patch("anthropic.Anthropic")
    def test_clean_chat_keeps_judges_score(self, mock_anthropic_class):
        from .views import score_single_chat
        mock_client = MagicMock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = self._judge_resp(
            "Both answers were correct and offered next steps.\nSCORE: 92")

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            chat = self._create_chat("clean-chat", [
                ("do you have charizard", "Yes -- Charizard VMAX, CAD 89.99.", DECKS_SHOWN),
                ("thanks", "Any time!", None),
            ])
            self.assertEqual(score_single_chat(chat), 92)

        chat.refresh_from_db()
        self.assertEqual(chat.evaluation_score, 92)
        self.assertEqual(chat.evaluation_evidence["caps_applied"], [])
        self.assertEqual(chat.evaluation_evidence["judge_score"], 92)
