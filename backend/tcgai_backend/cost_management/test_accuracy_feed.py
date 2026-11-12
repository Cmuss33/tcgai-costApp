"""Tests for GET /api/cost/accuracy/ -- the Accuracy Phase 1 feed.

AOP's Command Center reads this endpoint (session login OR the shared
X-CostApp-Token, the same scope as the commercial-impact endpoints) to
show the cost app's judge grades as the chatbot-accuracy source of truth:
a monthly chat-weighted mean series over the real-chat population, with
score bands, rubric versions, and a per-intent breakdown.

Population doctrine is inherited, not redefined: every number comes from
_chat_qs / _eval_avg / real_chats, so empty rows, diagnostic/verification
harness chats, probe-tagged chats, automated traffic and non-chat
surfaces are excluded exactly as the dashboard's headline excludes them.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Chat, Message

TOKEN = "accuracy-test-token"
URL = "/api/cost/accuracy/"


class AccuracyFeedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="pw")
        first = timezone.now().replace(day=1, hour=12, minute=0, second=0, microsecond=0)
        self.this_month = first
        self.prev_month = (first - timedelta(days=2)).replace(day=1)
        self.two_months_ago = (self.prev_month - timedelta(days=2)).replace(day=1)
        self._n = 0

    def _chat(self, when, score=None, rubric="v5", intent=None, shop="",
              with_message=True, **kwargs):
        self._n += 1
        chat = Chat.objects.create(
            chat_id=kwargs.pop("chat_id", f"acc-{self._n}"),
            model="claude-sonnet-5",
            shop=shop,
            evaluation_score=score,
            evaluation_rubric_version=rubric if score is not None else None,
            intent_primary_v3=intent,
            **kwargs,
        )
        if with_message:
            Message.objects.create(
                chat=chat, content="hi", llm_formatted_message="{}",
                returned_content="hello", llm_formatted_returned_message="{}",
                tokens_in=0, tokens_out=0, model="claude-sonnet-5",
            )
        Chat.objects.filter(pk=chat.pk).update(timestamp=when)
        return chat

    def _month(self, payload, month_start):
        label = f"{month_start:%Y-%m}"
        for entry in payload["months"]:
            if entry["month"] == label:
                return entry
        self.fail(f"month {label} missing from series: "
                  f"{[m['month'] for m in payload['months']]}")

    # --- Auth: session OR token, same 401 shape as commercial_impact ---

    def test_requires_login(self):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"error": "unauthenticated"})

    @override_settings(COSTAPP_API_TOKEN=TOKEN)
    def test_token_rejected_when_env_unset(self):
        with override_settings(COSTAPP_API_TOKEN=""):
            resp = self.client.get(URL, HTTP_X_COSTAPP_TOKEN=TOKEN)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"error": "unauthenticated"})

    @override_settings(COSTAPP_API_TOKEN=TOKEN)
    def test_wrong_token_rejected_with_same_shape_as_login(self):
        resp = self.client.get(URL, HTTP_X_COSTAPP_TOKEN="wrong-token")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"error": "unauthenticated"})
        bare = self.client.get(URL)
        self.assertEqual(resp.content, bare.content)

    @override_settings(COSTAPP_API_TOKEN=TOKEN)
    def test_correct_token_payload_matches_session_payload(self):
        self._chat(self.this_month.replace(day=5), score=80)
        token_resp = self.client.get(URL, HTTP_X_COSTAPP_TOKEN=TOKEN)
        self.assertEqual(token_resp.status_code, 200)

        self.client.force_login(self.user)
        session_resp = self.client.get(URL)
        self.assertEqual(session_resp.status_code, 200)

        token_body = token_resp.json()
        session_body = session_resp.json()
        token_body.pop("generated_at")
        session_body.pop("generated_at")
        self.assertEqual(token_body, session_body)

    def test_session_login_works(self):
        self.client.force_login(self.user)
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["attention_line"], 65)

    @override_settings(COSTAPP_API_TOKEN=TOKEN)
    def test_token_does_not_admit_post(self):
        resp = self.client.post(URL, HTTP_X_COSTAPP_TOKEN=TOKEN)
        self.assertEqual(resp.status_code, 401)

    # --- The series -----------------------------------------------------

    def test_monthly_series_is_chat_weighted(self):
        # One 100 on day 5, three 40s on day 6: chat-weighted mean is 55.0
        # (a mean of daily means would give 70.0). The feed must agree
        # with the dashboard headline's _eval_avg, not the old formula.
        self._chat(self.this_month.replace(day=5), score=100)
        for _ in range(3):
            self._chat(self.this_month.replace(day=6), score=40)
        self._chat(self.prev_month.replace(day=10), score=80)
        self._chat(self.prev_month.replace(day=11), score=90)
        self.client.force_login(self.user)

        payload = self.client.get(URL).json()

        labels = [m["month"] for m in payload["months"]]
        self.assertEqual(labels, sorted(labels))
        self.assertEqual(labels[0], f"{self.prev_month:%Y-%m}")
        self.assertEqual(labels[-1], f"{self.this_month:%Y-%m}")

        current = self._month(payload, self.this_month)
        self.assertEqual(current["scored_chats"], 4)
        self.assertEqual(current["mean_score"], 55.0)

        previous = self._month(payload, self.prev_month)
        self.assertEqual(previous["scored_chats"], 2)
        self.assertEqual(previous["mean_score"], 85.0)

        # Lifetime is the chat-weighted mean over every scored chat:
        # (100 + 3*40 + 80 + 90) / 6 = 65.0.
        self.assertEqual(payload["lifetime"], {"mean_score": 65.0, "scored_chats": 6})

    def test_series_starts_at_first_scored_month(self):
        # An unscored real chat two months ago must not pull the series
        # start earlier than the first month that has a grade.
        self._chat(self.two_months_ago.replace(day=10), score=None)
        self._chat(self.prev_month.replace(day=10), score=70)
        self.client.force_login(self.user)

        payload = self.client.get(URL).json()
        self.assertEqual(payload["months"][0]["month"], f"{self.prev_month:%Y-%m}")

    def test_empty_month_inside_range_renders_nulls(self):
        self._chat(self.two_months_ago.replace(day=10), score=70)
        self._chat(self.this_month.replace(day=10), score=90)
        self.client.force_login(self.user)

        entry = self._month(self.client.get(URL).json(), self.prev_month)
        self.assertIsNone(entry["mean_score"])
        self.assertEqual(entry["scored_chats"], 0)
        self.assertEqual(entry["below_attention"], 0)
        self.assertIsNone(entry["below_attention_pct"])
        self.assertEqual(entry["rubric_versions"], [])
        self.assertEqual(entry["intents"], [])
        self.assertEqual(
            entry["bands"],
            {"ge_90": 0, "75_89": 0, "65_74": 0, "40_64": 0, "lt_40": 0},
        )

    def test_no_scored_chats_at_all(self):
        self.client.force_login(self.user)
        payload = self.client.get(URL).json()
        self.assertEqual(payload["months"], [])
        self.assertEqual(payload["lifetime"], {"mean_score": None, "scored_chats": 0})

    # --- Population: exactly the dashboard headline's real chats --------

    def test_excluded_populations_do_not_count(self):
        when = self.this_month.replace(day=5)
        self._chat(when, score=50)  # the one genuine scored chat
        self._chat(when, score=None)  # unscored: never in a mean
        self._chat(when, score=100, with_message=False)  # empty row
        self._chat(when, score=100, chat_id="diagnostic-test-acc")
        self._chat(when, score=100, chat_id="claude-verification-acc")
        self._chat(when, score=100, is_probe=True)  # AOP probe replay
        self._chat(when, score=100, likely_automated=True)  # ENG-149/150 bot
        self._chat(when, score=100, chat_id="advisor_123", surface="advisor")
        self.client.force_login(self.user)

        payload = self.client.get(URL).json()
        current = self._month(payload, self.this_month)
        self.assertEqual(current["scored_chats"], 1)
        self.assertEqual(current["mean_score"], 50.0)
        self.assertEqual(payload["lifetime"]["scored_chats"], 1)

    # --- Breakdowns ------------------------------------------------------

    def test_bands_and_below_attention(self):
        when = self.this_month.replace(day=5)
        for score in (95, 90, 89, 75, 74, 65, 64, 40, 39, 10):
            self._chat(when, score=score)
        self.client.force_login(self.user)

        entry = self._month(self.client.get(URL).json(), self.this_month)
        self.assertEqual(entry["scored_chats"], 10)
        self.assertEqual(
            entry["bands"],
            {"ge_90": 2, "75_89": 2, "65_74": 2, "40_64": 2, "lt_40": 2},
        )
        self.assertEqual(sum(entry["bands"].values()), entry["scored_chats"])
        # Below the 65 attention line: the 40_64 and lt_40 bands.
        self.assertEqual(entry["below_attention"], 4)
        self.assertEqual(entry["below_attention_pct"], 40.0)

    def test_rubric_versions_reported(self):
        when = self.this_month.replace(day=5)
        self._chat(when, score=80, rubric="v5")
        self._chat(when, score=70, rubric="v4")
        self._chat(when, score=60, rubric="v5")
        self.client.force_login(self.user)

        entry = self._month(self.client.get(URL).json(), self.this_month)
        self.assertEqual(entry["rubric_versions"], ["v4", "v5"])

    def test_intent_breakdown(self):
        when = self.this_month.replace(day=5)
        self._chat(when, score=80, intent="product_lookup")
        self._chat(when, score=60, intent="product_lookup")
        self._chat(when, score=50, intent="store_policy")
        self._chat(when, score=100, intent=None)  # counts, but has no intent row
        self.client.force_login(self.user)

        entry = self._month(self.client.get(URL).json(), self.this_month)
        self.assertEqual(entry["scored_chats"], 4)
        by_intent = {row["intent"]: row for row in entry["intents"]}
        self.assertEqual(
            by_intent,
            {
                "product_lookup": {
                    "intent": "product_lookup", "mean_score": 70.0, "scored_chats": 2,
                },
                "store_policy": {
                    "intent": "store_policy", "mean_score": 50.0, "scored_chats": 1,
                },
            },
        )

    # --- Shop filter ------------------------------------------------------

    def test_shop_filter(self):
        when = self.this_month.replace(day=5)
        self._chat(when, score=100, shop="a.myshopify.com")
        self._chat(when, score=50, shop="b.myshopify.com")
        self.client.force_login(self.user)

        filtered = self.client.get(URL + "?shop=b.myshopify.com").json()
        entry = self._month(filtered, self.this_month)
        self.assertEqual(entry["scored_chats"], 1)
        self.assertEqual(entry["mean_score"], 50.0)
        self.assertEqual(filtered["lifetime"], {"mean_score": 50.0, "scored_chats": 1})

        unfiltered = self.client.get(URL).json()
        self.assertEqual(unfiltered["lifetime"]["scored_chats"], 2)

    # --- Consistency pins --------------------------------------------------

    def test_months_sum_to_lifetime_and_bands_sum_to_month(self):
        self._chat(self.two_months_ago.replace(day=3), score=95)
        self._chat(self.prev_month.replace(day=3), score=30)
        self._chat(self.prev_month.replace(day=4), score=70)
        self._chat(self.this_month.replace(day=3), score=64)
        self.client.force_login(self.user)

        payload = self.client.get(URL).json()
        self.assertEqual(
            sum(m["scored_chats"] for m in payload["months"]),
            payload["lifetime"]["scored_chats"],
        )
        for entry in payload["months"]:
            self.assertEqual(
                sum(entry["bands"].values()), entry["scored_chats"], entry["month"]
            )
            self.assertEqual(
                entry["below_attention"],
                entry["bands"]["40_64"] + entry["bands"]["lt_40"],
                entry["month"],
            )
