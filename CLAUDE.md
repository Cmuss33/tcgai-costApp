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
Requires a `.env` file (loaded via `python-dotenv`) with at least: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_ADMIN_KEY`. Optionally set `ANTHROPIC_TRACKED_API_KEY_IDS` (comma-separated Anthropic API key ids, e.g. `apikey_...,apikey_...`) to scope `get_tokens`/`get_model_rates`'s token counts to this app's own production keys instead of the whole org — recommended when the admin key's org has other keys unrelated to this chatbot. `ANTHROPIC_WORKSPACE_ID` is no longer read anywhere in this app (removed 2026-09-16, ENG-148 follow-up): confirmed live that Anthropic's cost_report reports `workspace_id: null` on real cache-cost line items even for keys that ARE workspace-scoped at creation, which silently zeroed out every Haiku cache rate whenever it was set — `usage_report`'s `api_key_id` field never comes back null, so key-id scoping replaced it everywhere workspace-id scoping used to apply.

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

**LLM provider adapter pattern**: `cost_management/api_clients.py` defines an abstract `LLMAdapter` (`get_cost`, `get_tokens`, `get_model_rates`). `cost_management/llm_provider_adapter_implementations.py` has the only implementation, `AnthropicAdapter`, which calls Anthropic's organization cost/usage-report admin APIs directly via `requests` (not the `anthropic` SDK — that's used separately in `views.py` for chat evaluation). `get_model_rates` derives a real effective $/token rate per model from the cost report (billed $) divided by the usage report (real token counts) — not a hardcoded price table. `get_tokens`/`get_model_rates` read `ANTHROPIC_TRACKED_API_KEY_IDS` (`_tracked_api_key_ids()`) and, when set, filter their usage-report rows client-side to those `api_key_id`s (grouped by `api_key_id`/`["model", "api_key_id"]` respectively — `usage_report` has no server-side per-key filter param, unlike its real `workspace_ids[]` filter which this app no longer uses). `get_cost`/`get_model_rates`'s cost-report call is always whole-org: `cost_report`'s `group_by[]` only ever accepts `description`/`workspace_id`, never `api_key_id`, so per-key cost scoping isn't possible there at all — and `workspace_id` scoping was dropped after it was confirmed to come back `null` on real cache-cost rows regardless. `get_usage_by_key` is deliberately never scoped by anything (its whole purpose is showing every key with usage, expected or not). `views.py` instantiates a single module-level `llmprovider = AnthropicAdapter()` and views call through it. Adding a provider means implementing `LLMAdapter` and swapping/adding the instantiation — there's no dynamic provider selection today.

**Data model** (`cost_management/models.py`): `Chat` (keyed by `chat_id`, a string from the upstream chatbot, not Django's default PK) has many `Message`s. `Chat` also caches running totals (`tokens_in`, `tokens_out`, `intent`, `evaluation_score`) that are updated incrementally in `log_message` rather than derived from `Message` aggregates — keep these in sync when touching that view. `log_message` is the ingestion endpoint an external chatbot service posts to; it silently rejects a hardcoded health-check probe payload (`content == 'hi this is the probe'`).

**Chat evaluation**: `evaluate_chat` sends the full conversation to `claude-haiku-4-5` with a fixed grading prompt and stores the returned score on `Chat.evaluation_score`. The averaging endpoints (`get_avg_eval_score`, `get_avg_tokens_in/out`, `get_avg_conversations_per_day`) all share the `get_period_start` helper (`"daily"` / `"7_days"` / default 30 days) and compute per-day aggregates via `TruncDate` before averaging — mirror this pattern for new time-windowed stats rather than filtering on raw timestamps.

**Frontend routing** (`frontend/src/App.jsx`): three routes — `/` (Login), `/cost` (CostView), `/chats` (ChatSummaryView) — with the shared `Header` hidden only on the login route. Each view independently reads `import.meta.env.VITE_API_URL` and does its own `auth-check/` fetch on mount rather than using shared auth context/state.
