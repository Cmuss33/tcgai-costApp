# Flag-conversations feature — Phase 1 deferred items

Recorded at the end of the Phase 1 subagent-driven build (branch
`feature/flag-conversations-investigation`, HEAD `121c65c`). The final
whole-branch review was clean after one fix wave; these items were
consciously deferred, not missed.

## Must fix before Phase 2

- **Retry-guard vs 409 ordering** (`cost_management/investigation_views.py`,
  `flag_chat`). The Linear-retry branch (`github_issue_number` set and
  `linear_issue_id` empty) is checked *before* the 409 "already
  flagged/resolved" check. In Phase 1 a `resolved` chat can only be reached
  via Django admin, so this is unreachable. Once Phase 2 makes `resolved` a
  normal state, a `resolved` chat with an empty `linear_issue_id` would
  wrongly enter `_create_linear_only` and create a second Linear issue.
  Reorder so the 409 check wins, or scope the retry branch to
  `investigation_status == "flagged"`.

## Adapter hardening (follow-up)

- `GitHubIssueTracker.create_issue` still does raw `data["node_id"]` /
  `data["number"]` / `data["html_url"]` after a successful `resp.json()`. A
  2xx with valid JSON but an unexpected shape raises a bare `KeyError` →
  unhandled 500. Network errors and JSON-parse errors are already wrapped as
  `IssueTrackerError` (fixed in the fix wave); this missing-key path is the
  remaining gap. Low likelihood — GitHub's 201 issue response is a stable
  documented contract.
- `_neutralize_fences` (`investigation_views.py`) substitutes triple
  backticks with U+02BC `ʼ`. Functionally fine; consider switching to ASCII
  `'''` for source cleanliness.

## Observability / robustness (follow-up)

- `_create_linear_only` uses a plain `chat.save()` rather than the
  `select_for_update` path in `_persist_flag`. Its success path also sets
  `flag_error = ""` unconditionally, which clears a prior *label*- or
  *comment*-failure note when only the Linear half is being retried. Low
  impact (single-reviewer, low contention).
- First-flag TOCTOU: two concurrent first-time `flag_chat` calls for the
  same chat both pass the pre-transaction `github_issue_number` check and
  could each create a GitHub issue. `select_for_update` in `_persist_flag`
  serializes the writes, not the create. The frontend button-disable plus
  the sequential 409 are the primary guards; a comment on `_persist_flag`
  documents this. Holding a row lock across two 10 s HTTP calls would be
  worse.
- `flag_chat` returns up to 500 chars of an external API's response body to
  the client (`detail` / `flag_error`), which can leak repo names and
  GraphQL internals to the browser. Acceptable for an internal authenticated
  dashboard; note it.

## Frontend polish (follow-up)

- The Retry modal pre-fills and lets the user edit the reason textarea, but
  the retry path (`_create_linear_only`) ignores the posted reason and
  rebuilds from `chat.flag_reason`. Make the textarea read-only in the retry
  case, or thread the edited reason through.
- `?chat=<id>` is never cleared from the URL. Closing the transcript modal
  leaves the param, so a later `searchParams` change re-opens it. Call
  `setSearchParams({}, {replace: true})` in `closeModal`.
- The `resolved` badge branch in `renderInvestigationCell` (and
  `.badge-resolved` CSS) is Phase 2 UI shipped early. Harmless and useful
  for the admin escape hatch — intentional, not an accident.

## Spec deviation (accepted)

- `cost_management/issue_trackers.py` has no `IssueTracker(ABC)` base class,
  though the spec's Components section specifies one "mirroring the
  `LLMAdapter` pattern". The two tracker classes are standalone. Defensible
  YAGNI — the only polymorphic method (`get_states`) is Phase 2 — but the
  module docstring's "mirrors the LLMAdapter pattern" line overstates what
  the code does. Add the ABC when `get_states` lands in Phase 2.

## Pre-existing, not introduced here

- Unauthenticated POSTs to `flag_chat` / other views get a 302 to a
  nonexistent login route (Django `login_required` default); the SPA
  surfaces this as a JSON parse error. App-wide pattern.
- `frontend` ESLint baseline has 2 errors (`src/login/login.jsx:50`,
  `vite.config.js:3`) and several `react-hooks/exhaustive-deps` warnings on
  untouched effects.

## Operator setup still required before the feature works in an environment

Set these backend env vars (see `CLAUDE.md`): `GITHUB_TOKEN` (fine-grained
PAT, Issues R/W + Metadata R on `professormeta/agentic-shopify-chatbot`),
`LINEAR_API_KEY`, `LINEAR_TEAM_ID`, `LINEAR_PROJECT_ID`,
`COST_APP_PUBLIC_URL`. `GITHUB_ISSUE_REPO` and `GITHUB_TRIGGER_LABEL` have
defaults. Ensure the `agent:queued` label exists in the target repo.
Resolve the Linear team/project IDs once via GraphQL (queries in
`CLAUDE.md`). The spec flags both external API payload shapes as unverified
against live APIs — do one smoke test against a scratch repo before the
first real flag (a wrong field name currently surfaces as a clean 502).
