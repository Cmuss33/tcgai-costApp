# Flag conversations for investigation — design

**Date:** 2026-08-28
**Status:** Approved for implementation planning
**Author:** brainstormed with Claude Code

## Problem

Reviewers looking at logged chatbot conversations in the TCGai Cost App have no
way to escalate a conversation that looks wrong. They want to:

1. Flag a specific conversation, stating **why** it needs investigation.
2. Have that flag automatically open a **GitHub issue** in
   `professormeta/agentic-shopify-chatbot`, worded so the repo's existing
   `Agent — Analyse Issue` workflow picks it up and starts an automated
   investigation using the stated reason.
3. Have a **Linear issue** created in the `professor-meta` workspace's
   `shopify-chatbot` project, cross-linked with the GitHub issue.
4. See, back in the dashboard, when a flagged conversation has been **resolved**
   (truly fixed, not just triaged).

## Constraints and context

- Backend: Django 5, plain `JsonResponse` views (no DRF), session-cookie auth,
  `@login_required`. External HTTP via `requests` (already a dependency).
- No task queue (no Celery/RQ) in the stack. External calls happen
  synchronously inside the request.
- Frontend: React 19 + Vite SPA. No JS test runner configured.
- `requirements.txt` is UTF-16 — but this feature adds **no** new Python
  packages, so it is not touched.
- The target repo's workflow (`Agent — Analyse Issue`) triggers on:
  - `issues.opened` **and** issue author is `OWNER`/`MEMBER`/`COLLABORATOR`, **or**
  - `issues.labeled` with label name `agent:queued`.
  It reads `ISSUE_TITLE` and `ISSUE_BODY` and passes them to `analyse.js`. It
  dedups concurrent runs via an `agent:analysing` label.
- The two external API payload shapes (GitHub REST, Linear GraphQL) were **not**
  verified against live APIs during design. Implementation builds them from
  official docs and keeps them fixture-driven so a wrong field name is a
  one-place fix.

## State model

`Chat` gains an investigation lifecycle:

```
unflagged  ──flag (issues created)──▶  flagged  ──GitHub issue closed──▶  resolved
                                          ▲                                   │
                                          └───────GitHub issue reopened───────┘
```

- `unflagged` — default, nothing done.
- `flagged` — both issues created (or GitHub created and Linear pending retry).
- `resolved` — the linked GitHub issue is closed.
- Reopening the GitHub issue moves `resolved` → `flagged`.
- There is **no** "unflag" and **no** UI "mark resolved" button. A chat that is
  a non-issue is corrected via Django admin (escape hatch).
- Linear issue state does **not** affect the lifecycle. GitHub is the single
  source of truth for "resolved".

### New `Chat` fields (migration `0009_chat_investigation_fields`)

| field | type | notes |
|---|---|---|
| `investigation_status` | `CharField(max_length=20, choices=..., default='unflagged')` | `unflagged` / `flagged` / `resolved` |
| `flag_reason` | `TextField(blank=True, default='')` | reviewer's verbatim text |
| `flagged_at` | `DateTimeField(null=True, blank=True)` | set when first flagged |
| `flagged_by` | `CharField(max_length=150, blank=True, default='')` | `request.user.username` |
| `github_issue_number` | `IntegerField(null=True, blank=True)` | for webhook + reconcile matching |
| `github_issue_url` | `URLField(blank=True, default='')` | row link |
| `linear_issue_id` | `CharField(max_length=64, blank=True, default='')` | Linear UUID |
| `linear_issue_url` | `URLField(blank=True, default='')` | row link |
| `flag_error` | `TextField(blank=True, default='')` | last partial/total failure; `''` when clean (lever 2) |

`get_chat_ids` serializes with `.values()`, so all new fields reach the
frontend automatically. No serialization code change beyond confirming the keys
appear.

Register all new fields in `cost_management/admin.py` as
`list_display` / editable so state can be inspected and hand-fixed (lever 6).

## Components

### 1. `cost_management/issue_trackers.py` (new)

Mirrors the `LLMAdapter` pattern in `api_clients.py`.

```
IssueTracker(ABC)
    create_issue(title, body, labels=None) -> IssueRef
    add_label(ref, label) -> None
    add_comment(ref, body) -> None
    get_states(numbers_or_ids) -> dict            # id/number -> "open" | "closed"

IssueRef = dataclass(id: str, number: int | None, url: str)
```

**`GitHubIssueTracker(IssueTracker)`**

- Base: `https://api.github.com`, repo from `settings.GITHUB_ISSUE_REPO`.
- Headers: `Authorization: Bearer <GITHUB_TOKEN>`,
  `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`.
- `create_issue`: `POST /repos/{repo}/issues` `{title, body}` →
  `IssueRef(id=node_id, number=number, url=html_url)`.
- `add_label`: `POST /repos/{repo}/issues/{number}/labels` `{labels:[label]}`.
- `add_comment`: `POST /repos/{repo}/issues/{number}/comments` `{body}`.
- `get_states`: `GET /repos/{repo}/issues/{number}` per number (small N),
  map `state` field. Batched caller decides caching.
- All calls: `timeout=10`. Non-2xx raises `IssueTrackerError` carrying the
  status code and a response-body snippet.
- Handles GitHub secondary-rate-limit `403` / `429` by raising
  `IssueTrackerError` with a clear message (no retry loop in v1).

**`LinearIssueTracker(IssueTracker)`**

- Endpoint: `https://api.linear.app/graphql`.
- Header: `Authorization: <LINEAR_API_KEY>` — personal API keys are sent
  **raw, without `Bearer`**. (Verify during implementation; this is a common
  gotcha.)
- `create_issue`: mutation
  ```graphql
  mutation IssueCreate($input: IssueCreateInput!) {
    issueCreate(input: $input) { success issue { id identifier url } }
  }
  ```
  input `{ teamId: settings.LINEAR_TEAM_ID, projectId: settings.LINEAR_PROJECT_ID,
  title, description }`. `labels` param ignored for Linear in v1.
- `get_states`: query `issue(id:...) { state { type } }`; map
  `type in ("completed", "canceled")` → `"closed"`, else `"open"`.
  (Not used by the lifecycle, but implemented for symmetry / future use — may be
  cut if it adds cost; see YAGNI.)
- `timeout=10`. `errors` array in response or non-2xx → `IssueTrackerError`.

Module-level singletons, like `llmprovider`:
```python
github_tracker = GitHubIssueTracker()
linear_tracker = LinearIssueTracker()
```

**Resolving `LINEAR_TEAM_ID` / `LINEAR_PROJECT_ID`:** a one-off during
implementation — query `teams { nodes { id name } }` and
`projects { nodes { id name } }` with the real key, put the IDs in `.env`, and
document them in `CLAUDE.md`. Not done dynamically at runtime.

### 2. `cost_management/investigation_views.py` (new)

Keeps this feature out of the already-long `views.py`.

#### `flag_chat(request)` — `@login_required`, `@csrf_exempt`, `POST`

Body: `{ "chat_id": str, "reason": str }`.

Preflight (lever 5): if any of `GITHUB_TOKEN`, `GITHUB_ISSUE_REPO`,
`LINEAR_API_KEY`, `LINEAR_TEAM_ID`, `LINEAR_PROJECT_ID`, `COST_APP_PUBLIC_URL`
is missing/empty → `503 {"error": "investigation integration not configured",
"missing": ["LINEAR_TEAM_ID", ...]}`. No external calls attempted.

Validation:
- `reason` blank/whitespace → `400`.
- `chat_id` not found → `404`.
- Chat already `flagged`/`resolved` **with** a `linear_issue_id` → `409`
  (nothing to do).
- Chat already `flagged` **without** `linear_issue_id` → **Linear-retry
  branch**: skip GitHub, run only the Linear half (step 4–6 below), update
  fields, clear/set `flag_error`, return `200`.

Idempotency (lever 3): wrap the status transition in
`transaction.atomic()` + `Chat.objects.select_for_update().get(chat_id=...)`,
re-check status inside the lock. If `github_issue_number` is already set, never
create a second GitHub issue.

Happy path:

1. Load `Chat` + `Message`s ordered by `timestamp`.
2. Compose **title**: `Investigate chat {chat_id}: {reason[:60]}` (collapse
   newlines, ellipsis if truncated).
3. Compose **body** (Markdown):
   ```
   ## Flag reason
   {reason verbatim}

   ## Chat metadata
   - chat_id: {chat_id}
   - intent: {intent}
   - eval score: {evaluation_score or "not evaluated"}
   - tokens in / out: {tokens_in} / {tokens_out}
   - model: {model}
   - first seen: {timestamp ISO}
   - flagged by: {username} at {flagged_at ISO}
   - Cost app: {COST_APP_PUBLIC_URL}/chats?chat={chat_id}

   ## Transcript
   **User:** {content}
   **Assistant:** {returned_content}
   ... (repeated per message, in order)
   ```
   Transcript uses `Message.content` / `Message.returned_content`. If a message
   has empty `content` (post-GDPR role-only entries), render `*(no user text
   recorded)*`.
4. **GitHub create**: `github_tracker.create_issue(title, body)`.
   - Failure → `502 {"error": ..., "detail": snippet}`; **nothing persisted**;
     chat stays `unflagged`. (lever: total failure is clean.)
5. **GitHub trigger label**: `github_tracker.add_label(ref,
   settings.GITHUB_TRIGGER_LABEL)` (default `agent:queued`, lever 4).
   - Failure → **non-fatal**. Append to `flag_error`
     (`"trigger label failed: ..."`), continue.
6. **Linear create**: `linear_tracker.create_issue(title, linear_body)` where
   `linear_body` = the same body **plus** a leading line
   `GitHub issue: {github_issue_url}` (feeds Linear's native GitHub
   integration + explicit cross-link).
   - Failure → persist GitHub fields, `investigation_status='flagged'`,
     `flag_error = "linear create failed: ..."`, return
     `200 {"investigation_status":"flagged", "github_issue_url":...,
     "linear_error": snippet}`. Row shows `⚠ Retry`.
7. **GitHub back-link comment**: `github_tracker.add_comment(ref,
   "Linked Linear issue: {linear_issue_url}")`.
   - Failure → non-fatal, append to `flag_error`.
8. Persist (single `save(update_fields=[...])`): `investigation_status`,
   `flag_reason`, `flagged_at` (only if not already set), `flagged_by`,
   `github_issue_number`, `github_issue_url`, `linear_issue_id`,
   `linear_issue_url`, `flag_error` (`''` if all clean).
9. Return `200 {"investigation_status": "flagged", "github_issue_url": ...,
   "linear_issue_url": ..., "flag_error": ""}` (or with the soft-warning text).

Structured logging (lever, debuggability): every external call logs one line
prefixed `[investigation]` with `chat_id`, operation, target URL, HTTP status,
and — on error — a response snippet.

#### `github_webhook(request)` — `@csrf_exempt`, `POST`, no auth

- Read raw body. Compute `hmac.new(GITHUB_WEBHOOK_SECRET, body, sha256)`,
  compare `sha256=<hex>` to `X-Hub-Signature-256` with
  `hmac.compare_digest`. Mismatch/missing → `403`.
- Parse JSON. Only handle `X-GitHub-Event: issues`.
  - `action == "closed"` →
    `Chat.objects.filter(github_issue_number=number,
    investigation_status="flagged").update(investigation_status="resolved")`.
  - `action == "reopened"` →
    `... investigation_status="resolved" ... update(investigation_status="flagged")`.
  - anything else → no-op.
- Always return `200` (so GitHub does not auto-disable the hook), except the
  `403` signature failure.
- Log `[investigation] webhook action=... issue=... matched=<n>`.

### 3. Reconcile fallback (in `views.py::get_chat_ids`)

After `results` is assembled, before returning:

- Collect `github_issue_number` for rows where
  `investigation_status == "flagged"` and the number is set.
- If none → skip.
- Cache key `investigation:reconcile` in `django.core.cache` with a
  ~300s TTL holding `{number: state}`. On miss, call
  `github_tracker.get_states(numbers)` and store.
- For any number whose state is `"closed"`:
  `Chat.objects.filter(github_issue_number=number,
  investigation_status="flagged").update(investigation_status="resolved")` and
  patch the matching dict in `results`.
- Entire block wrapped in `try/except Exception` that logs `[investigation]
  reconcile failed: ...` and continues. **Never** raises into the response.

`django.core.cache` default is local-memory per-process — acceptable; worst
case each web worker makes one call per 5 min.

### 4. URLs (`cost_management/urls.py`)

```python
path('flag_chat/', investigation_views.flag_chat, name='flag_chat'),
path('github_webhook/', investigation_views.github_webhook, name='github_webhook'),
```

### 5. Settings (`tcgai_backend/settings.py`)

Read into module constants near the bottom:

```python
GITHUB_TOKEN          = os.environ.get('GITHUB_TOKEN', '')
GITHUB_ISSUE_REPO     = os.environ.get('GITHUB_ISSUE_REPO', 'professormeta/agentic-shopify-chatbot')
GITHUB_TRIGGER_LABEL  = os.environ.get('GITHUB_TRIGGER_LABEL', 'agent:queued')
GITHUB_WEBHOOK_SECRET = os.environ.get('GITHUB_WEBHOOK_SECRET', '')
LINEAR_API_KEY        = os.environ.get('LINEAR_API_KEY', '')
LINEAR_TEAM_ID        = os.environ.get('LINEAR_TEAM_ID', '')
LINEAR_PROJECT_ID     = os.environ.get('LINEAR_PROJECT_ID', '')
COST_APP_PUBLIC_URL   = os.environ.get('COST_APP_PUBLIC_URL', '')
```

Document all eight in `CLAUDE.md` (backend env section).

### 6. Frontend (`frontend/src/chatSummary/`)

**`ChatSummaryView.jsx`**

- New table column **"Investigation"** (header + cell).
- Cell renders by `chat.investigation_status`:
  - `unflagged` (or missing) → `<button class="flag-button">🚩 Flag</button>`.
  - `flagged` → `<span class="badge badge-flagged">Flagged</span>` +
    `GitHub ↗` / `Linear ↗` anchors (`target="_blank"`,
    `rel="noopener noreferrer"`). If `chat.flag_error` is non-empty, show
    `⚠ Retry` instead of/next to the badge, wired to re-open the modal.
  - `resolved` → `<span class="badge badge-resolved">Resolved ✓</span>` + links.
- `flag` button opens `<FlagChatModal>` for that `chat_id`.
- `flagChat(chatId, reason)`:
  `POST ${API_URL}/api/cost/flag_chat/`, `credentials:"include"`,
  `Content-Type: application/json`, body `{chat_id, reason}`. On `200`, merge
  the returned fields into that row in `chats` state. On non-2xx, surface the
  error text inside the modal; leave it open.
- Deep link: on mount, read `?chat=` from `window.location.search`
  (`useSearchParams` from `react-router-dom`). If present, call the existing
  `openChatModal(id)` once chats are loaded.

**`FlagChatModal.jsx`** (new, sibling of the existing modal markup)

- Props: `chatId`, `onSubmit(reason)`, `onClose`, `pending`, `error`.
- Controlled `<textarea>` for the reason; `Submit` disabled while
  `reason.trim() === ""` or `pending`. Spinner while `pending`. Inline
  `error` display. `Cancel` calls `onClose`.
- Follows existing modal styling conventions.

**`ChatSummaryView.css`** — add `.flag-button`, `.badge`, `.badge-flagged`,
`.badge-resolved`, `.retry-link`, and flag-modal rules, reusing existing
modal/overlay classes where possible.

## Error handling summary

| Failure | HTTP | Persisted | Chat status | Row shows |
|---|---|---|---|---|
| Integration env var missing | 503 | none | unchanged | (button, error toast) |
| Blank reason / bad chat | 400 / 404 | none | unchanged | modal error |
| Already flagged + has Linear id | 409 | none | unchanged | badge (no-op) |
| GitHub create fails | 502 | none | `unflagged` | button + error |
| Trigger label fails | 200 | yes | `flagged` | `Flagged` + `flag_error` note |
| Linear create fails | 200 | GitHub fields | `flagged` | `⚠ Retry` |
| Back-link comment fails | 200 | yes | `flagged` | `Flagged` + `flag_error` note |
| Webhook bad signature | 403 | none | unchanged | — |

No retry loops, no queue. Recovery from a partial failure = reviewer clicks
`Flag` / `Retry` again (Linear-retry branch), or a maintainer fixes state in
Django admin.

## Testing (TDD — test written first for every backend change)

Backend `cost_management/tests.py`, Django `TestCase`, `requests` calls patched
with `unittest.mock` at the `issue_trackers` boundary. Fixtures for GitHub /
Linear responses live in the test module so real-shape corrections are
one place.

Model / migration:
- `investigation_status` defaults to `unflagged`; choices enforced.
- New fields nullable/blank as specified; `get_chat_ids` output includes them.

`flag_chat`:
- Happy path: both trackers called; all fields persisted; `flagged_at` set;
  `investigation_status='flagged'`; issue body contains the reason and the
  full transcript (assert on a substring of each message).
- Preflight: missing env var → 503 with `missing` list; no tracker calls.
- Blank/whitespace reason → 400.
- Unknown `chat_id` → 404.
- Already flagged + `linear_issue_id` set → 409; no tracker calls.
- Linear-retry branch: already `flagged`, `linear_issue_id` empty →
  only Linear called; `linear_issue_*` filled; `flag_error` cleared.
- GitHub create raises → 502; `Chat` row unchanged (re-fetch and assert).
- Trigger-label raises → 200; `investigation_status='flagged'`;
  `flag_error` contains "trigger label".
- Linear create raises → 200; GitHub fields persisted;
  response has `linear_error`; `flag_error` contains "linear".
- Back-link comment raises → 200; persisted; `flag_error` contains "comment".
- Double-submit (two calls, second while first "in flight" simulated) creates
  only one GitHub issue (assert `create_issue` called once).

`github_webhook`:
- Valid signature + `action=closed` + matching `github_issue_number` →
  chat becomes `resolved`; response 200.
- Valid signature + `action=reopened` on a `resolved` chat → back to `flagged`.
- Invalid/missing signature → 403; no state change.
- `action=edited` (or non-`issues` event) → 200 no-op.
- `closed` with no matching chat → 200, `matched=0`.

`get_chat_ids` reconcile:
- `flagged` chat whose mocked `get_states` returns `closed` → row returned as
  `resolved` and DB updated.
- `get_states` raises → listing still returns 200 with rows unchanged.
- Second call within TTL does not re-hit `get_states` (assert call count).

Frontend: no test runner in repo. Manual verification checklist in the plan +
`npm run lint` must pass. (Adding Vitest is out of scope unless requested.)

## Files

**New**
- `backend/tcgai_backend/cost_management/issue_trackers.py`
- `backend/tcgai_backend/cost_management/investigation_views.py`
- `backend/tcgai_backend/cost_management/migrations/0009_chat_investigation_fields.py`
- `frontend/src/chatSummary/FlagChatModal.jsx`

**Modified**
- `backend/tcgai_backend/cost_management/models.py` — new `Chat` fields
- `backend/tcgai_backend/cost_management/admin.py` — register new fields
- `backend/tcgai_backend/cost_management/urls.py` — 2 routes
- `backend/tcgai_backend/cost_management/views.py` — reconcile block in `get_chat_ids`
- `backend/tcgai_backend/cost_management/tests.py` — new test classes
- `backend/tcgai_backend/tcgai_backend/settings.py` — 8 env constants
- `frontend/src/chatSummary/ChatSummaryView.jsx` — column, modal wiring, deep link
- `frontend/src/chatSummary/ChatSummaryView.css` — badges, button, modal
- `CLAUDE.md` — document new env vars + Linear ID lookup note

**Not touched**
- `requirements.txt` (no new packages — uses `requests`)

## External setup (operator, out of code)

1. Fine-grained GitHub PAT: Issues R/W + Metadata R on
   `professormeta/agentic-shopify-chatbot` → `GITHUB_TOKEN`.
2. Ensure the `agent:queued` label exists in that repo (the workflow's
   `labeled` trigger needs the exact name).
3. GitHub repo webhook → `https://<backend>/api/cost/github_webhook/`,
   content-type `application/json`, secret = `GITHUB_WEBHOOK_SECRET`,
   events = **Issues** only.
4. Linear personal API key → `LINEAR_API_KEY`; resolve + set `LINEAR_TEAM_ID`
   and `LINEAR_PROJECT_ID` (shopify-chatbot project).
5. `COST_APP_PUBLIC_URL` = deployed frontend origin.
6. Confirm Linear's GitHub integration is installed on the workspace (for the
   native auto-link; the explicit URL cross-link works regardless).

## YAGNI — explicitly out of scope

- Task queue / async retries / cron poller.
- UI "unflag" and UI "mark resolved" buttons.
- Editing the reason after submission.
- Linear webhook (GitHub is the sole resolve signal).
- `LinearIssueTracker.get_states` may be dropped if it is not needed by any
  caller (lifecycle does not use it).
- Frontend unit tests / Vitest setup.
- Per-user permissions beyond the existing `@login_required`.

## Open risks

1. **GitHub / Linear payload shapes unverified live.** Mitigation:
   fixture-driven, single-file corrections, first real call surfaces any
   mismatch immediately.
2. **Trigger assumption.** If the repo renames `agent:queued` or changes
   `author_association` gating, our issue is created but no investigation
   starts. Mitigation: `GITHUB_TRIGGER_LABEL` is configurable; the issue still
   exists and can be labeled by hand.
3. **Local-memory cache** means reconcile fan-out scales with worker count.
   Acceptable at current scale; revisit if workers multiply.
4. **Webhook secret rotation** requires updating both the repo config and the
   env var together.
