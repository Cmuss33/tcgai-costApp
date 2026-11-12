"""Tests for GET /api/cost/accuracy/chats/ -- the Accuracy Phase 2 feed.

The row-level companion to the Phase 1 monthly series (test_accuracy_feed.py):
AOP's fix attribution forms date-windowed cohorts of chats by intent and
joins them against its own issue/evaluation records by conversation id,
which monthly aggregates cannot do. Auth is the same session-or-token
scope as the accuracy feed.

Population doctrine is inherited, not redefined: the queryset is the
accuracy view's population verbatim (real_chats + shop filter + scored
filter), so empty rows, diagnostic/verification harness chats,
probe-tagged chats, automated traffic, non-chat surfaces and unscored
chats are absent exactly as they are from the aggregates -- and a test
below pins that per-month counts from these rows equal the series'
scored_chats.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Chat, Message

TOKEN = "test-token-accuracy-chats"
URL = "/api/cost/accuracy/chats/"
SERIES_URL = "/api/cost/accuracy/"


class AccuracyChatsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="pw-accuracy-chats")
        first = timezone.now().replace(day=1, hour=12, minute=0, second=0, microsecond=0)
        self.this_month = first
        self.prev_month = (first - timedelta(days=2)).replace(day=1)
        self._n = 0

    def _chat(self, when, score=None, rubric="v5", intent=None, shop="",
              with_message=True, **kwargs):
        self._n += 1
        chat = Chat.objects.create(
            chat_id=kwargs.pop("chat_id", f"acc-chats-{self._n}"),
            model="claude-sonnet-5",
            shop=shop,
            evaluation_score=score,
            evaluation_rubric_version=rubric,
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

    def _rows(self, url=URL):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.json()["chats"]

    # --- Auth: session OR token, same 401 shape as the accuracy feed ----

    def test_requires_login(self):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"error": "unauthenticated"})

    @override_settings(COSTAPP_API_TOKEN="")
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
        self.assertEqual(resp.json()["chats"], [])

    @override_settings(COSTAPP_API_TOKEN=TOKEN)
    def test_token_does_not_admit_post(self):
        resp = self.client.post(URL, HTTP_X_COSTAPP_TOKEN=TOKEN)
        self.assertEqual(resp.status_code, 401)

    # --- Row shape --------------------------------------------------------

    def test_row_shape_and_fields(self):
        when = self.this_month.replace(day=5, hour=9, minute=30)
        chat = self._chat(when, score=80, rubric="v5", intent="product_lookup")
        self.client.force_login(self.user)

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0],
            {
                "chat_id": chat.chat_id,
                "date": when.date().isoformat(),
                "month": f"{when:%Y-%m}",
                "score": 80,
                "rubric_version": "v5",
                "intent": "product_lookup",
            },
        )
        self.assertEqual(rows[0]["month"], rows[0]["date"][:7])

    def test_intent_null_when_absent(self):
        when = self.this_month.replace(day=5)
        self._chat(when, score=70, intent=None)
        self._chat(when, score=60, intent="")
        self.client.force_login(self.user)

        rows = self._rows()
        self.assertEqual([r["intent"] for r in rows], [None, None])

    def test_rubric_version_null_when_absent(self):
        # A pre-receipts scored row carries no rubric stamp; the feed
        # reports the stored value (null), never an invented version.
        self._chat(self.this_month.replace(day=5), score=70, rubric=None)
        self.client.force_login(self.user)

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["rubric_version"])

    # --- Population: exactly the accuracy series' scored real chats ------

    def test_excluded_populations_do_not_appear(self):
        when = self.this_month.replace(day=5)
        genuine = self._chat(when, score=50)  # the one genuine scored chat
        self._chat(when, score=None)  # unscored: no grade to report
        self._chat(when, score=100, with_message=False)  # empty row
        self._chat(when, score=100, chat_id="diagnostic-test-acc-chats")
        self._chat(when, score=100, chat_id="claude-verification-acc-chats")
        self._chat(when, score=100, is_probe=True)  # AOP probe replay
        self._chat(when, score=100, likely_automated=True)  # ENG-149/150 bot
        self._chat(when, score=100, chat_id="advisor_456", surface="advisor")
        self.client.force_login(self.user)

        rows = self._rows()
        self.assertEqual([r["chat_id"] for r in rows], [genuine.chat_id])

    # --- Ordering ----------------------------------------------------------

    def test_ordering_by_date_then_chat_id(self):
        day5 = self.this_month.replace(day=5, hour=10)
        day3 = self.this_month.replace(day=3, hour=10)
        # Inserted newest-first on purpose; the feed must sort, not
        # inherit insertion order. The two day-5 rows share a timestamp,
        # so chat_id breaks the tie.
        self._chat(day5, score=70, chat_id="zz-tie")
        self._chat(day5, score=60, chat_id="aa-tie")
        self._chat(day3, score=80, chat_id="mm-earlier")
        self.client.force_login(self.user)

        rows = self._rows()
        self.assertEqual(
            [(r["date"], r["chat_id"]) for r in rows],
            [
                (day3.date().isoformat(), "mm-earlier"),
                (day5.date().isoformat(), "aa-tie"),
                (day5.date().isoformat(), "zz-tie"),
            ],
        )

    # --- Shop filter -------------------------------------------------------

    def test_shop_filter(self):
        when = self.this_month.replace(day=5)
        self._chat(when, score=100, shop="a.myshopify.com", chat_id="shop-a-row")
        self._chat(when, score=50, shop="b.myshopify.com", chat_id="shop-b-row")
        self.client.force_login(self.user)

        filtered = self._rows(URL + "?shop=b.myshopify.com")
        self.assertEqual([r["chat_id"] for r in filtered], ["shop-b-row"])

        unfiltered = self._rows()
        self.assertEqual(
            {r["chat_id"] for r in unfiltered}, {"shop-a-row", "shop-b-row"}
        )

    # --- Consistency with the Phase 1 aggregates ---------------------------

    def test_per_month_counts_match_accuracy_series(self):
        self._chat(self.prev_month.replace(day=3), score=95)
        self._chat(self.prev_month.replace(day=4), score=30)
        self._chat(self.this_month.replace(day=3), score=64)
        self._chat(self.this_month.replace(day=4), score=88)
        self._chat(self.this_month.replace(day=5), score=None)  # unscored
        self._chat(self.this_month.replace(day=5), score=100, is_probe=True)
        self.client.force_login(self.user)

        rows = self._rows()
        counts = {}
        for r in rows:
            counts[r["month"]] = counts.get(r["month"], 0) + 1

        series = self.client.get(SERIES_URL).json()
        series_counts = {
            m["month"]: m["scored_chats"]
            for m in series["months"]
            if m["scored_chats"]
        }
        self.assertEqual(counts, series_counts)
        self.assertEqual(len(rows), series["lifetime"]["scored_chats"])

    def test_month_boundary_consistency(self):
        # The last minute of the previous month and the first minute of
        # this one: each chat's `month` here must be the month it counts
        # toward in the aggregates (Chat.timestamp bucketing, UTC).
        first_instant = self.this_month.replace(hour=0, minute=0, second=0, microsecond=0)
        before = self._chat(first_instant - timedelta(minutes=1), score=70)
        after = self._chat(first_instant + timedelta(minutes=1), score=90)
        self.client.force_login(self.user)

        rows = {r["chat_id"]: r for r in self._rows()}
        self.assertEqual(rows[before.chat_id]["month"], f"{self.prev_month:%Y-%m}")
        self.assertEqual(
            rows[before.chat_id]["date"],
            (first_instant - timedelta(minutes=1)).date().isoformat(),
        )
        self.assertEqual(rows[after.chat_id]["month"], f"{self.this_month:%Y-%m}")

        series = self.client.get(SERIES_URL).json()
        by_month = {m["month"]: m["scored_chats"] for m in series["months"]}
        self.assertEqual(by_month[f"{self.prev_month:%Y-%m}"], 1)
        self.assertEqual(by_month[f"{self.this_month:%Y-%m}"], 1)
