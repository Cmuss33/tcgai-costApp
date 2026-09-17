# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

TCGai Cost App: a dashboard for monitoring Anthropic API cost/token usage and reviewing logged chatbot conversations. Two independent projects in one repo, deployed separately (Render, per `ALLOWED_HOSTS`/CORS entries in settings.py):

- `backend/tcgai_backend/` — Django 5 REST-ish API (no DRF, plain `JsonResponse` views)
- `frontend/` — React 19 + Vite SPA

## Commands

### Backend (run from `backend/tcgai_backend/`)
```bash
python -m venv venv && source venv/bin/activate   # first time
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver                         # dev server on :8000
python manage.py test                               # run tests
python manage.py test cost_management.tests.SomeTestCase.test_name  # single test
python manage.py makemigrations cost_management     # after model changes
```
Requires a `.env` file (loaded via `python-dotenv`) with at least: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_ADMIN_KEY`. Optionally set `ANTHROPIC_APP_API_KEY_IDS` (comma-separated Anthropic `api_key_id` values — this app's own production keys, past and present, e.g. an old pre-rotation shared key plus the current per-surface keys) to scope `get_cost`/`get_tokens`'s **absolute totals** to just this app. Required in practice: this Anthropic org also holds other, unrelated projects (confirmed live 2026-09-16: Claude Code, BudgetETL, Photo Highlights, Roblox Studio workspaces) whose spend silently inflated the dashboard's whole-org total by ~$37 before this was added. `get_model_rates` deliberately does **not** read this var — it derives a $/token *unit rate*, not an absolute total, and Anthropic prices uniformly per model/token-type regardless of which key made the call, so scoping it would only bias the ratio (see the incident below). `ANTHROPIC_WORKSPACE_ID` is no longer read anywhere — retracted earlier the same day (ENG-148 follow-up) after `cost_report` was found to report `workspace_id: null` on real cache-cost line items even for workspace-scoped keys, silently zeroing out every Haiku cache rate whenever it was set.

Two things were tried and retracted before `ANTHROPIC_APP_API_KEY_IDS` landed, both same day (2026-09-16):
1. `ANTHROPIC_WORKSPACE_ID` scoping (see above).
2. Scoping `get_model_rates`' `usage_report` token side to a handful of key ids while its `cost_report` $ side necessarily stayed whole-org (no per-key filter exists there) — broke the numerator/denominator population match a real rate requires. Caught live right after a key rotation: the new keys had only hours of traffic against a full month of org-wide spend, inflating one chat's estimated cost ~1500x ($35 for ~53k Haiku tokens that should cost about $0.02).

See `WholeOrgRateDerivationTests` and `AppApiKeyScopingTests` in `cost_management/tests.py` for the regression coverage from both incidents. The `AnthropicAdapter.get_usage_by_key` *adapter method* was never scoped by any of this — its whole purpose is showing every key with usage, expected or not. Its caller, `stats_views.usage_by_key` (the "Usage by API key" dashboard panel), does filter down to `ANTHROPIC_APP_API_KEY_IDS` when set — same list, applied one layer up, so that panel reads as "our keys" and its rows sum to the same `ANTHROPIC_APP_API_KEY_IDS`-scoped "Anthropic spend" total shown elsewhere on the page. As with `get_cost`/`get_tokens`, this means rotated-out keys must stay in the list or they silently drop off the panel.

For the conversation-flagging feature (Phase 1) also set: `GITHUB_TOKEN` (fine-grained PAT, Issues read/write + Metadata read on the issue repo), `GITHUB_ISSUE_REPO` (defaults to `professormeta/agentic-shopify-chatbot`), `GITHUB_TRIGGER_LABEL` (defaults to `agent:queued`), `LINEAR_API_KEY`, `LINEAR_TEAM_ID`, `LINEAR_PROJECT_ID`, and `COST_APP_PUBLIC_URL` (deployed frontend origin, used to build deep links in issues). Resolve the Linear team/project IDs once with a GraphQL call — `query { teams { nodes { id name } } }` and `query { projects { nodes { id name } } }` — then paste the IDs into `.env`.

`requirements.txt` is UTF-16 encoded — edit with a tool that preserves that encoding, or re-save as UTF-16 after editing.

### Frontend (run from `frontend/`)
```bash
npm install
npm run dev       # Vite dev server, default :5173
npm run build     # production build
npm run lint      # ESLint
npm run preview   # preview production build
```
Requires `VITE_API_URL` (e.g. in `.env`) pointing at the backend origin (e.g. `http://127.0.0.1:8000`).

## Architecture

**Auth**: Django session-cookie auth (`django.contrib.auth`), not token-based. Login is `POST /api/cost/login/`; the frontend calls `auth-check/` on each page and always sends `credentials: "include"` on fetches. Cookies are configured `Secure`/`SameSite=None`, so local HTTP-only dev requires care (see commented-out HTTPS/proxy block in `frontend/vite.config.js`) — cross-origin cookies won't work over plain `http://localhost`.

**LLM provider adapter pattern**: `cost_management/api_clients.py` defines an abstract `LLMAdapter` (`get_cost`, `get_tokens`, `get_model_rates`). `cost_management/llm_provider_adapter_implementations.py` has the only implementation, `AnthropicAdapter`, which calls Anthropic's organization cost/usage-report admin APIs directly via `requests` (not the `anthropic` SDK — that's used separately in `views.py` for chat evaluation). Two different scoping rules apply, and mixing them up is exactly what caused a live ~1500x cost-estimate inflation (2026-09-16) — see the `.env` section above for the full incident history:
- `get_model_rates` derives a real effective $/token *unit rate* per model from the cost report (billed $) divided by the usage report (real token counts) — not a hardcoded price table. Both sides are always whole-org/unfiltered: `cost_report`'s `group_by[]` only ever accepts `description`/`workspace_id`, never `api_key_id`, so per-key cost scoping isn't possible there at all — and since Anthropic prices uniformly per model/token-type across the org, a whole-org ratio *is* the correct unit rate regardless of which keys generated the traffic. Scoping only one side of the ratio (tried twice: by workspace, then by key id) breaks the numerator/denominator population match a real rate requires.
- `get_cost`/`get_tokens` report **absolute totals** for this app specifically, which — unlike a unit rate — really do need scoping: this Anthropic org also holds other, unrelated projects (Claude Code, BudgetETL, Photo Highlights, Roblox Studio), so a whole-org total silently included ~$37 of unrelated spend. `get_tokens` filters `usage_report` rows by the real, reliable `api_key_id` field directly. `get_cost` can't do the equivalent against `cost_report` (no per-key filter exists there at all), so it instead multiplies `get_model_rates`' whole-org unit rates by this app's own `api_key_id`-scoped token counts — internally calling `self.get_model_rates(...)` and making its own separate `usage_report` request, the same estimation technique `stats_views.usage_by_key` already uses per individual key, applied here to the whole app's total. Both read `ANTHROPIC_APP_API_KEY_IDS`.

`get_usage_by_key` (adapter method) is deliberately never scoped by anything (its whole purpose is showing every key with usage, expected or not) — the app-key filter for the dashboard panel lives in `stats_views.usage_by_key` instead (see the `.env` section above). Each of its per-model token buckets tracks `uncached_input_tokens`/`output_tokens`/`cache_creation_tokens`/`cache_read_tokens` separately (ENG-148 parity fix — it used to drop cache tokens entirely, which meant the per-key cost estimate in `stats_views.usage_by_key` could never sum to the cache-inclusive "Anthropic spend" KPI); a key's top-level `input_tokens` is the true total (uncached + both cache directions), matching `get_tokens`. The `app_api_key_ids()` helper (public, despite living in the adapter module) is shared by `get_cost`/`get_tokens` and `stats_views.usage_by_key` so the key-id list is parsed in exactly one place. `views.py` instantiates a single module-level `llmprovider = AnthropicAdapter()` and views call through it. Adding a provider means implementing `LLMAdapter` and swapping/adding the instantiation — there's no dynamic provider selection today.

**Data model** (`cost_management/models.py`): `Chat` (keyed by `chat_id`, a string from the upstream chatbot, not Django's default PK) has many `Message`s. `Chat` also caches running totals (`tokens_in`, `tokens_out`, `intent`, `evaluation_score`) that are updated incrementally in `log_message` rather than derived from `Message` aggregates — keep these in sync when touching that view. `log_message` is the ingestion endpoint an external chatbot service posts to; it silently rejects a hardcoded health-check probe payload (`content == 'hi this is the probe'`).

**Chat evaluation**: `evaluate_chat` sends the full conversation to `claude-haiku-4-5` with a fixed grading prompt and stores the returned score on `Chat.evaluation_score`. The averaging endpoints (`get_avg_eval_score`, `get_avg_tokens_in/out`, `get_avg_conversations_per_day`) all share the `get_period_start` helper (`"daily"` / `"7_days"` / default 30 days) and compute per-day aggregates via `TruncDate` before averaging — mirror this pattern for new time-windowed stats rather than filtering on raw timestamps.

**Frontend routing** (`frontend/src/App.jsx`): three routes — `/` (Login), `/cost` (CostView), `/chats` (ChatSummaryView) — with the shared `Header` hidden only on the login route. Each view independently reads `import.meta.env.VITE_API_URL` and does its own `auth-check/` fetch on mount rather than using shared auth context/state.
