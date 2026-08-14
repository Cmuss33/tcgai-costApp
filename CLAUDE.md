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
Requires a `.env` file (loaded via `python-dotenv`) with at least: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_ADMIN_KEY`. `requirements.txt` is UTF-16 encoded — edit with a tool that preserves that encoding, or re-save as UTF-16 after editing.

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

**LLM provider adapter pattern**: `cost_management/api_clients.py` defines an abstract `LLMAdapter` (`get_cost`, `get_tokens`). `cost_management/llm_provider_adapter_implementations.py` has the only implementation, `AnthropicAdapter`, which calls Anthropic's organization cost/usage-report admin APIs directly via `requests` (not the `anthropic` SDK — that's used separately in `views.py` for chat evaluation). `views.py` instantiates a single module-level `llmprovider = AnthropicAdapter()` and views call through it. Adding a provider means implementing `LLMAdapter` and swapping/adding the instantiation — there's no dynamic provider selection today.

**Data model** (`cost_management/models.py`): `Chat` (keyed by `chat_id`, a string from the upstream chatbot, not Django's default PK) has many `Message`s. `Chat` also caches running totals (`tokens_in`, `tokens_out`, `intent`, `evaluation_score`) that are updated incrementally in `log_message` rather than derived from `Message` aggregates — keep these in sync when touching that view. `log_message` is the ingestion endpoint an external chatbot service posts to; it silently rejects a hardcoded health-check probe payload (`content == 'hi this is the probe'`).

**Chat evaluation**: `evaluate_chat` sends the full conversation to `claude-haiku-4-5` with a fixed grading prompt and stores the returned score on `Chat.evaluation_score`. The averaging endpoints (`get_avg_eval_score`, `get_avg_tokens_in/out`, `get_avg_conversations_per_day`) all share the `get_period_start` helper (`"daily"` / `"7_days"` / default 30 days) and compute per-day aggregates via `TruncDate` before averaging — mirror this pattern for new time-windowed stats rather than filtering on raw timestamps.

**Frontend routing** (`frontend/src/App.jsx`): three routes — `/` (Login), `/cost` (CostView), `/chats` (ChatSummaryView) — with the shared `Header` hidden only on the login route. Each view independently reads `import.meta.env.VITE_API_URL` and does its own `auth-check/` fetch on mount rather than using shared auth context/state.
