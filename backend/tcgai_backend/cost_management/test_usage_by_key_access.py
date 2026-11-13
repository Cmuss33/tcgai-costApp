"""Auth-scope tests for GET /api/cost/get_usage_by_key/.

The endpoint's data behaviour is covered by UsageByKeyEndpointTests /
UsageByKeyUntrackedTests in tests.py. This file covers only the auth
scope change that lets AOP's Command Center read it: session login OR
the shared X-CostApp-Token (api_login_or_token_required), the wiring
src/aiCosts.js in the AOP repo always promised ("When a cost-app API
credential exists, wire it here"). The token path must stay GET-only,
must stay disabled while COSTAPP_API_TOKEN is unset, and a bad token
must get the exact 401 the login path returns.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings

TOKEN = "aop-command-center-token"
URL = "/api/cost/get_usage_by_key/"


class UsageByKeyTokenAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="pw")
        p = patch.multiple(
            "cost_management.views.llmprovider",
            get_usage_by_key=MagicMock(return_value={
                "keys": [{
                    "api_key_id": "apikey_chat", "name": "ANTHROPIC_API_KEY_CHATBOT",
                    "input_tokens": 1000, "output_tokens": 500,
                    "by_model": {},
                }],
                "workspace_id": None,
            }),
            get_model_rates=MagicMock(return_value={"rates": {}}),
        )
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        cache.clear()

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
        resp = self.client.get(URL, HTTP_X_COSTAPP_TOKEN="tok-789")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"error": "unauthenticated"})
        bare = self.client.get(URL)
        self.assertEqual(resp.content, bare.content)

    @override_settings(COSTAPP_API_TOKEN=TOKEN)
    def test_correct_token_payload_matches_session_payload(self):
        token_resp = self.client.get(URL, HTTP_X_COSTAPP_TOKEN=TOKEN)
        self.assertEqual(token_resp.status_code, 200)

        self.client.force_login(self.user)
        session_resp = self.client.get(URL)
        self.assertEqual(session_resp.status_code, 200)

        token_body = token_resp.json()
        session_body = session_resp.json()
        # The monthly branch serves the second read from cache and flags it.
        token_body.pop("cached", None)
        session_body.pop("cached", None)
        self.assertEqual(token_body, session_body)
        self.assertEqual(token_body["keys"][0]["api_key_id"], "apikey_chat")
        self.assertIn("untracked_keys", token_body)

    @override_settings(COSTAPP_API_TOKEN=TOKEN)
    def test_token_is_get_only(self):
        resp = self.client.post(URL, HTTP_X_COSTAPP_TOKEN=TOKEN)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"error": "unauthenticated"})
