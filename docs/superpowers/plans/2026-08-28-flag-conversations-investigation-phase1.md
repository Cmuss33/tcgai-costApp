# Flag Conversations for Investigation — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a reviewer flag a logged chat with a written reason, which creates a GitHub issue (labelled to start the repo's Analyse-Issue workflow) and a cross-linked Linear issue, and shows the flag state on the chat row.

**Architecture:** A new `Chat` lifecycle field (`investigation_status`) plus issue metadata columns. A new `issue_trackers.py` module holds two thin `requests`-based adapters (`GitHubIssueTracker`, `LinearIssueTracker`) mirroring the existing `LLMAdapter` pattern. A new `investigation_views.py` holds the `flag_chat` POST endpoint that composes the issue body (reason + metadata + full transcript), calls GitHub then Linear, cross-links them, and persists the result — with partial-failure handling so a Linear outage still records the GitHub issue. The frontend adds one table column with a Flag button and a reason modal.

**Tech Stack:** Django 5 (plain `JsonResponse` views, no DRF), `requests` (already a dependency — no new packages), Postgres, React 19 + Vite, `react-router-dom` 7.

**Spec:** `docs/superpowers/specs/2026-08-28-flag-conversations-investigation-design.md` — read it alongside this plan. This plan implements only the sections tagged **[P1]**.

## Global Constraints

- **No new Python packages.** Use `requests` (already in `requirements.txt`). Do not edit `requirements.txt` (it is UTF-16).
- **View conventions:** decorate `flag_chat` with `@login_required`, `@csrf_exempt`, `@require_http_methods(["POST"])` — exactly like `evaluate_chat` in `cost_management/views.py`. Return `django.http.JsonResponse`.
- **Frontend fetch conventions:** every request sends `credentials: "include"`; the API base is `import.meta.env.VITE_API_URL`; endpoints are under `${API_URL}/api/cost/`.
- **TDD, every backend task:** write the failing test → run it, confirm it fails for the expected reason → write the minimal implementation → run it, confirm pass → commit.
- **Migration `0009` defines all three `investigation_status` values** (`unflagged`, `flagged`, `resolved`) even though Phase 1 only ever sets `unflagged`/`flagged` — Phase 2 must need no migration.
- **`GITHUB_TRIGGER_LABEL`** defaults to the string `agent:queued`.
- **Structured logging:** every external call logs one line via the module `logger`, message prefixed `[investigation]`, including `chat_id`, the operation, and (on error) a response snippet.
- **External HTTP:** `timeout=10` on every call; any non-2xx response raises `IssueTrackerError`.
- **Backend test command (PowerShell, run from `backend/tcgai_backend/`):** `python manage.py test cost_management` (single test: `python manage.py test cost_management.tests.ClassName.test_name`). Tests create a `test_` Postgres database — the configured DB user needs create-database permission (the existing `cost_management/tests.py` already relies on this).
- **Frontend checks (run from `frontend/`):** `npm run lint` and `npm run build` must both pass. There is no JS test runner; the frontend task uses a manual verification checklist.
- **Commit after every task** (and after each green test cycle within a task). Branch is `feature/flag-conversations-investigation`, already checked out.

---

## File Structure

**New files:**

| File | Responsibility |
|---|---|
| `backend/tcgai_backend/cost_management/issue_trackers.py` | `IssueTrackerError`, `IssueRef` dataclass, `GitHubIssueTracker`, `LinearIssueTracker`, and the module-level `github_tracker` / `linear_tracker` singletons. Pure external-API wrappers — no Django model access. |
| `backend/tcgai_backend/cost_management/investigation_views.py` | The `flag_chat` view plus private helpers (`_missing_settings`, `_build_title`, `_build_issue_body`, `_persist_flag`, `_create_linear_only`). |
| `backend/tcgai_backend/cost_management/migrations/0009_chat_investigation_fields.py` | Adds the nine investigation fields to `Chat`. |
| `frontend/src/chatSummary/FlagChatModal.jsx` | Controlled reason-entry modal. Presentational: receives `pending`/`error`, emits `onSubmit(reason)` / `onClose`. |

**Modified files:**

| File | Change |
|---|---|
| `cost_management/models.py` | Nine new `Chat` fields + a `INVESTIGATION_STATUS_CHOICES` constant. |
| `cost_management/admin.py` | Register `Chat` with the investigation fields in `list_display` / `list_editable`. |
| `cost_management/urls.py` | One route: `flag_chat/`. |
| `tcgai_backend/settings.py` | Seven env-var constants. |
| `cost_management/tests.py` | New test classes for the trackers and the view. |
| `frontend/src/chatSummary/ChatSummaryView.jsx` | New "Investigation" column, flag modal wiring, `?chat=` deep link. |
| `frontend/src/chatSummary/ChatSummaryView.css` | Button, badge, issue-link, and flag-modal rules. |
| `CLAUDE.md` | Document the seven new backend env vars + the one-off Linear ID lookup. |

---

## Task 1: `Chat` investigation fields + migration + admin

**Files:**
- Modify: `backend/tcgai_backend/cost_management/models.py` (the `Chat` class, ends at line 22)
- Create: `backend/tcgai_backend/cost_management/migrations/0009_chat_investigation_fields.py`
- Modify: `backend/tcgai_backend/cost_management/admin.py`
- Test: `backend/tcgai_backend/cost_management/tests.py`

**Interfaces:**
- Produces:
  - `Chat.investigation_status: str` — one of `"unflagged"` (default) / `"flagged"` / `"resolved"`
  - `Chat.flag_reason: str` (default `""`)
  - `Chat.flagged_at: datetime | None`
  - `Chat.flagged_by: str` (default `""`)
  - `Chat.github_issue_number: int | None`
  - `Chat.github_issue_url: str` (default `""`)
  - `Chat.linear_issue_id: str` (default `""`)
  - `Chat.linear_issue_url: str` (default `""`)
  - `Chat.flag_error: str` (default `""`)
  - `Chat.INVESTIGATION_STATUS_CHOICES: list[tuple[str, str]]`
  - All nine fields appear in `get_chat_ids` JSON rows (that view uses `Chat.objects...values()`, so no view change is needed — the test below locks it in).

- [ ] **Step 1: Write the failing tests**

Add to `backend/tcgai_backend/cost_management/tests.py` (append at end of file; keep the existing imports, add `from django.utils import timezone` if not present):

```python
class ChatInvestigationFieldsTests(TestCase):
    def test_new_chat_defaults_to_unflagged(self):
        chat = Chat.objects.create(chat_id="conv-defaults", model="claude-haiku-4-5")
        self.assertEqual(chat.investigation_status, "unflagged")
        self.assertEqual(chat.flag_reason, "")
        self.assertIsNone(chat.flagged_at)
        self.assertEqual(chat.flagged_by, "")
        self.assertIsNone(chat.github_issue_number)
        self.assertEqual(chat.github_issue_url, "")
        self.assertEqual(chat.linear_issue_id, "")
        self.assertEqual(chat.linear_issue_url, "")
        self.assertEqual(chat.flag_error, "")

    def test_status_choices_are_the_three_lifecycle_values(self):
        values = [value for value, _label in Chat.INVESTIGATION_STATUS_CHOICES]
        self.assertEqual(values, ["unflagged", "flagged", "resolved"])


class GetChatIdsInvestigationFieldsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(self.user)

    def test_get_chat_ids_row_includes_investigation_fields(self):
        Chat.objects.create(
            chat_id="conv-inv",
            model="claude-haiku-4-5",
            investigation_status="flagged",
            flag_reason="looks wrong",
            github_issue_url="https://github.com/x/y/issues/1",
            linear_issue_url="https://linear.app/x/issue/ABC-1",
        )

        response = self.client.get("/api/cost/get_chat_ids/")

        row = next(c for c in response.json()["results"] if c["chat_id"] == "conv-inv")
        self.assertEqual(row["investigation_status"], "flagged")
        self.assertEqual(row["flag_reason"], "looks wrong")
        self.assertEqual(row["github_issue_url"], "https://github.com/x/y/issues/1")
        self.assertEqual(row["linear_issue_url"], "https://linear.app/x/issue/ABC-1")
```

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `python manage.py test cost_management.tests.ChatInvestigationFieldsTests cost_management.tests.GetChatIdsInvestigationFieldsTests`
Expected: FAIL — `AttributeError: type object 'Chat' has no attribute 'INVESTIGATION_STATUS_CHOICES'` / `TypeError` on unknown kwargs.

- [ ] **Step 3: Add the fields to the `Chat` model**

In `cost_management/models.py`, replace the `Chat` class body (lines 12–22) with:

```python
class Chat(models.Model):
    INVESTIGATION_STATUS_CHOICES = [
        ("unflagged", "Unflagged"),
        ("flagged", "Flagged"),
        ("resolved", "Resolved"),
    ]

    chat_id = models.CharField(max_length=255, primary_key=True)
    model = models.TextField()
    tokens_in = models.IntegerField(default=0)
    tokens_out = models.IntegerField(default=0)
    intent = models.TextField(default='NOT FOUND')
    timestamp = models.DateTimeField(auto_now_add=True)
    evaluation_score = models.IntegerField(null=True, blank=True)

    investigation_status = models.CharField(
        max_length=20,
        choices=INVESTIGATION_STATUS_CHOICES,
        default="unflagged",
    )
    flag_reason = models.TextField(blank=True, default="")
    flagged_at = models.DateTimeField(null=True, blank=True)
    flagged_by = models.CharField(max_length=150, blank=True, default="")
    github_issue_number = models.IntegerField(null=True, blank=True)
    github_issue_url = models.URLField(blank=True, default="")
    linear_issue_id = models.CharField(max_length=64, blank=True, default="")
    linear_issue_url = models.URLField(blank=True, default="")
    flag_error = models.TextField(blank=True, default="")

    def __str__(self):
        return self.chat_id
```

- [ ] **Step 4: Generate the migration**

Run: `python manage.py makemigrations cost_management`
Expected: creates `cost_management/migrations/0009_chat_investigation_fields.py` with nine `AddField` operations and `dependencies = [('cost_management', '0008_message_products_shown')]`.

If prompted for a name, it is fine to accept the default; then rename the file to `0009_chat_investigation_fields.py` and update the class if Django used a different suffix. The generated file should look like:

```python
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cost_management', '0008_message_products_shown'),
    ]

    operations = [
        migrations.AddField(model_name='chat', name='investigation_status',
            field=models.CharField(choices=[('unflagged', 'Unflagged'), ('flagged', 'Flagged'), ('resolved', 'Resolved')], default='unflagged', max_length=20)),
        migrations.AddField(model_name='chat', name='flag_reason', field=models.TextField(blank=True, default='')),
        migrations.AddField(model_name='chat', name='flagged_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='chat', name='flagged_by', field=models.CharField(blank=True, default='', max_length=150)),
        migrations.AddField(model_name='chat', name='github_issue_number', field=models.IntegerField(blank=True, null=True)),
        migrations.AddField(model_name='chat', name='github_issue_url', field=models.URLField(blank=True, default='')),
        migrations.AddField(model_name='chat', name='linear_issue_id', field=models.CharField(blank=True, default='', max_length=64)),
        migrations.AddField(model_name='chat', name='linear_issue_url', field=models.URLField(blank=True, default='')),
        migrations.AddField(model_name='chat', name='flag_error', field=models.TextField(blank=True, default='')),
    ]
```

- [ ] **Step 5: Register `Chat` in the admin**

Replace the whole of `cost_management/admin.py` with:

```python
from django.contrib import admin

from .models import Chat


@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
    list_display = (
        "chat_id",
        "investigation_status",
        "flagged_by",
        "flagged_at",
        "github_issue_number",
        "evaluation_score",
        "timestamp",
    )
    list_editable = ("investigation_status",)
    list_filter = ("investigation_status", "model")
    search_fields = ("chat_id", "flag_reason", "github_issue_url", "linear_issue_url")
    readonly_fields = ("chat_id", "timestamp")
```

- [ ] **Step 6: Run the tests, confirm they pass**

Run: `python manage.py test cost_management.tests.ChatInvestigationFieldsTests cost_management.tests.GetChatIdsInvestigationFieldsTests`
Expected: PASS (4 tests).

- [ ] **Step 7: Run the full app test suite to confirm nothing regressed**

Run: `python manage.py test cost_management`
Expected: PASS (all pre-existing tests still green).

- [ ] **Step 8: Commit**

```bash
git add backend/tcgai_backend/cost_management/models.py backend/tcgai_backend/cost_management/admin.py backend/tcgai_backend/cost_management/migrations/0009_chat_investigation_fields.py backend/tcgai_backend/cost_management/tests.py
git commit -m "Add Chat investigation lifecycle fields, migration, admin"
```

---

## Task 2: `GitHubIssueTracker`

**Files:**
- Create: `backend/tcgai_backend/cost_management/issue_trackers.py`
- Modify: `backend/tcgai_backend/tcgai_backend/settings.py`
- Test: `backend/tcgai_backend/cost_management/tests.py`

**Interfaces:**
- Consumes: `settings.GITHUB_TOKEN`, `settings.GITHUB_ISSUE_REPO` (added in this task).
- Produces:
  - `IssueTrackerError(Exception)` with attributes `.tracker: str`, `.operation: str`, `.status: int | None`, `.detail: str`.
  - `IssueRef` dataclass: `id: str`, `number: int | None`, `url: str`.
  - `GitHubIssueTracker` with:
    - `create_issue(title: str, body: str, labels: list[str] | None = None) -> IssueRef` (`.number` is the issue number, `.id` is `node_id`, `.url` is `html_url`)
    - `add_label(ref: IssueRef, label: str) -> None`
    - `add_comment(ref: IssueRef, body: str) -> None`
  - Module singleton `github_tracker = GitHubIssueTracker()`.

- [ ] **Step 1: Add the GitHub settings constants**

Append to the end of `tcgai_backend/settings.py`:

```python
# -----------------------------
# Investigation issue trackers (Phase 1)
# -----------------------------
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_ISSUE_REPO = os.environ.get('GITHUB_ISSUE_REPO', 'professormeta/agentic-shopify-chatbot')
GITHUB_TRIGGER_LABEL = os.environ.get('GITHUB_TRIGGER_LABEL', 'agent:queued')
LINEAR_API_KEY = os.environ.get('LINEAR_API_KEY', '')
LINEAR_TEAM_ID = os.environ.get('LINEAR_TEAM_ID', '')
LINEAR_PROJECT_ID = os.environ.get('LINEAR_PROJECT_ID', '')
COST_APP_PUBLIC_URL = os.environ.get('COST_APP_PUBLIC_URL', '')
```

(`os` is already imported at the top of `settings.py`.)

- [ ] **Step 2: Write the failing tests**

Append to `cost_management/tests.py`. Add `from unittest.mock import patch, MagicMock` to the imports if not present.

```python
from django.test import override_settings

from .issue_trackers import GitHubIssueTracker, IssueRef, IssueTrackerError


def _fake_response(status_code, json_body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


@override_settings(GITHUB_TOKEN="tok", GITHUB_ISSUE_REPO="acme/widgets")
class GitHubIssueTrackerTests(TestCase):
    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_posts_payload_and_parses_ref(self, mock_post):
        mock_post.return_value = _fake_response(
            201,
            {
                "number": 42,
                "node_id": "I_abc",
                "html_url": "https://github.com/acme/widgets/issues/42",
            },
        )

        ref = GitHubIssueTracker().create_issue("A title", "A body")

        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        self.assertEqual(url, "https://api.github.com/repos/acme/widgets/issues")
        self.assertEqual(kwargs["json"], {"title": "A title", "body": "A body"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(ref, IssueRef(id="I_abc", number=42,
                                       url="https://github.com/acme/widgets/issues/42"))

    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_raises_on_non_2xx(self, mock_post):
        mock_post.return_value = _fake_response(422, text="Validation failed")

        with self.assertRaises(IssueTrackerError) as ctx:
            GitHubIssueTracker().create_issue("t", "b")

        self.assertEqual(ctx.exception.tracker, "github")
        self.assertEqual(ctx.exception.operation, "create_issue")
        self.assertEqual(ctx.exception.status, 422)
        self.assertIn("Validation failed", ctx.exception.detail)

    @patch("cost_management.issue_trackers.requests.post")
    def test_add_label_posts_to_labels_endpoint(self, mock_post):
        mock_post.return_value = _fake_response(200, [])
        ref = IssueRef(id="I_abc", number=42, url="https://github.com/acme/widgets/issues/42")

        GitHubIssueTracker().add_label(ref, "agent:queued")

        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        self.assertEqual(url, "https://api.github.com/repos/acme/widgets/issues/42/labels")
        self.assertEqual(kwargs["json"], {"labels": ["agent:queued"]})

    @patch("cost_management.issue_trackers.requests.post")
    def test_add_comment_posts_body(self, mock_post):
        mock_post.return_value = _fake_response(201, {"id": 1})
        ref = IssueRef(id="I_abc", number=42, url="https://github.com/acme/widgets/issues/42")

        GitHubIssueTracker().add_comment(ref, "Linked Linear issue: https://linear.app/x/ABC-1")

        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        self.assertEqual(url, "https://api.github.com/repos/acme/widgets/issues/42/comments")
        self.assertEqual(kwargs["json"], {"body": "Linked Linear issue: https://linear.app/x/ABC-1"})

    @patch("cost_management.issue_trackers.requests.post")
    def test_add_label_raises_on_error(self, mock_post):
        mock_post.return_value = _fake_response(404, text="Not Found")
        ref = IssueRef(id="I_abc", number=42, url="u")

        with self.assertRaises(IssueTrackerError):
            GitHubIssueTracker().add_label(ref, "agent:queued")
```

- [ ] **Step 3: Run the tests, confirm they fail**

Run: `python manage.py test cost_management.tests.GitHubIssueTrackerTests`
Expected: FAIL — `ModuleNotFoundError: No module named 'cost_management.issue_trackers'`.

- [ ] **Step 4: Create `issue_trackers.py` with the GitHub adapter**

Create `backend/tcgai_backend/cost_management/issue_trackers.py`:

```python
"""External issue-tracker adapters for the investigation-flag feature.

Mirrors the LLMAdapter pattern in api_clients.py: thin wrappers over the
GitHub REST API and the Linear GraphQL API using `requests`. No Django model
access happens here.
"""

import json
import logging
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
LINEAR_API = "https://api.linear.app/graphql"
HTTP_TIMEOUT = 10


class IssueTrackerError(Exception):
    def __init__(self, tracker, operation, status, detail):
        self.tracker = tracker
        self.operation = operation
        self.status = status
        self.detail = detail
        super().__init__(f"{tracker}.{operation} failed (status={status}): {detail}")


@dataclass
class IssueRef:
    id: str
    number: "int | None"
    url: str


class GitHubIssueTracker:
    def _headers(self):
        return {
            "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _repo_url(self, suffix):
        return f"{GITHUB_API}/repos/{settings.GITHUB_ISSUE_REPO}{suffix}"

    def _check(self, resp, operation):
        if resp.status_code >= 300:
            raise IssueTrackerError("github", operation, resp.status_code, resp.text[:500])

    def create_issue(self, title, body, labels=None):
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        resp = requests.post(
            self._repo_url("/issues"),
            headers=self._headers(),
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
        self._check(resp, "create_issue")
        data = resp.json()
        return IssueRef(id=data["node_id"], number=data["number"], url=data["html_url"])

    def add_label(self, ref, label):
        resp = requests.post(
            self._repo_url(f"/issues/{ref.number}/labels"),
            headers=self._headers(),
            json={"labels": [label]},
            timeout=HTTP_TIMEOUT,
        )
        self._check(resp, "add_label")

    def add_comment(self, ref, body):
        resp = requests.post(
            self._repo_url(f"/issues/{ref.number}/comments"),
            headers=self._headers(),
            json={"body": body},
            timeout=HTTP_TIMEOUT,
        )
        self._check(resp, "add_comment")


github_tracker = GitHubIssueTracker()
```

- [ ] **Step 5: Run the tests, confirm they pass**

Run: `python manage.py test cost_management.tests.GitHubIssueTrackerTests`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/tcgai_backend/cost_management/issue_trackers.py backend/tcgai_backend/tcgai_backend/settings.py backend/tcgai_backend/cost_management/tests.py
git commit -m "Add GitHubIssueTracker adapter and investigation settings"
```

---

## Task 3: `LinearIssueTracker`

**Files:**
- Modify: `backend/tcgai_backend/cost_management/issue_trackers.py`
- Modify: `CLAUDE.md`
- Test: `backend/tcgai_backend/cost_management/tests.py`

**Interfaces:**
- Consumes: `settings.LINEAR_API_KEY`, `settings.LINEAR_TEAM_ID`, `settings.LINEAR_PROJECT_ID`; `IssueRef`, `IssueTrackerError` from Task 2.
- Produces:
  - `LinearIssueTracker` with `create_issue(title: str, body: str, labels: list[str] | None = None) -> IssueRef` — the returned `IssueRef` has `number=None`, `id` = Linear issue UUID, `url` = Linear issue URL.
  - Module singleton `linear_tracker = LinearIssueTracker()`.

- [ ] **Step 1: Write the failing tests**

Append to `cost_management/tests.py`:

```python
from .issue_trackers import LinearIssueTracker


@override_settings(LINEAR_API_KEY="lin_key", LINEAR_TEAM_ID="team-123", LINEAR_PROJECT_ID="proj-456")
class LinearIssueTrackerTests(TestCase):
    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_sends_mutation_and_parses_ref(self, mock_post):
        mock_post.return_value = _fake_response(
            200,
            {
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {
                            "id": "uuid-1",
                            "identifier": "SHО-7",
                            "url": "https://linear.app/professor-meta/issue/SHO-7",
                        },
                    }
                }
            },
        )

        ref = LinearIssueTracker().create_issue("A title", "A body")

        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        self.assertEqual(url, "https://api.linear.app/graphql")
        self.assertEqual(kwargs["headers"]["Authorization"], "lin_key")
        self.assertEqual(kwargs["timeout"], 10)
        variables = kwargs["json"]["variables"]["input"]
        self.assertEqual(variables["teamId"], "team-123")
        self.assertEqual(variables["projectId"], "proj-456")
        self.assertEqual(variables["title"], "A title")
        self.assertEqual(variables["description"], "A body")
        self.assertIn("issueCreate", kwargs["json"]["query"])
        self.assertEqual(ref.id, "uuid-1")
        self.assertIsNone(ref.number)
        self.assertEqual(ref.url, "https://linear.app/professor-meta/issue/SHO-7")

    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_raises_on_graphql_errors(self, mock_post):
        mock_post.return_value = _fake_response(
            200, {"errors": [{"message": "project not found"}]}
        )

        with self.assertRaises(IssueTrackerError) as ctx:
            LinearIssueTracker().create_issue("t", "b")

        self.assertEqual(ctx.exception.tracker, "linear")
        self.assertIn("project not found", ctx.exception.detail)

    @patch("cost_management.issue_trackers.requests.post")
    def test_create_issue_raises_on_http_error(self, mock_post):
        mock_post.return_value = _fake_response(401, text="Unauthorized")

        with self.assertRaises(IssueTrackerError) as ctx:
            LinearIssueTracker().create_issue("t", "b")

        self.assertEqual(ctx.exception.status, 401)
```

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `python manage.py test cost_management.tests.LinearIssueTrackerTests`
Expected: FAIL — `ImportError: cannot import name 'LinearIssueTracker'`.

- [ ] **Step 3: Add the Linear adapter**

In `cost_management/issue_trackers.py`, insert before the `github_tracker = ...` line:

```python
class LinearIssueTracker:
    _MUTATION = (
        "mutation IssueCreate($input: IssueCreateInput!) {"
        "  issueCreate(input: $input) {"
        "    success issue { id identifier url }"
        "  }"
        "}"
    )

    def _headers(self):
        return {
            "Authorization": settings.LINEAR_API_KEY,
            "Content-Type": "application/json",
        }

    def create_issue(self, title, body, labels=None):
        variables = {
            "input": {
                "teamId": settings.LINEAR_TEAM_ID,
                "projectId": settings.LINEAR_PROJECT_ID,
                "title": title,
                "description": body,
            }
        }
        resp = requests.post(
            LINEAR_API,
            headers=self._headers(),
            json={"query": self._MUTATION, "variables": variables},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code >= 300:
            raise IssueTrackerError("linear", "create_issue", resp.status_code, resp.text[:500])
        data = resp.json()
        if data.get("errors"):
            raise IssueTrackerError(
                "linear", "create_issue", resp.status_code, json.dumps(data["errors"])[:500]
            )
        issue = data["data"]["issueCreate"]["issue"]
        return IssueRef(id=issue["id"], number=None, url=issue["url"])
```

And add, right after the `github_tracker = GitHubIssueTracker()` line:

```python
linear_tracker = LinearIssueTracker()
```

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `python manage.py test cost_management.tests.LinearIssueTrackerTests`
Expected: PASS (3 tests).

- [ ] **Step 5: Document the new env vars in `CLAUDE.md`**

In `CLAUDE.md`, in the "Backend (run from `backend/tcgai_backend/`)" section, the line currently reads:

> Requires a `.env` file (loaded via `python-dotenv`) with at least: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_ADMIN_KEY`.

Replace it with:

```
Requires a `.env` file (loaded via `python-dotenv`) with at least: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_ADMIN_KEY`.

For the conversation-flagging feature (Phase 1) also set: `GITHUB_TOKEN` (fine-grained PAT, Issues read/write + Metadata read on the issue repo), `GITHUB_ISSUE_REPO` (defaults to `professormeta/agentic-shopify-chatbot`), `GITHUB_TRIGGER_LABEL` (defaults to `agent:queued`), `LINEAR_API_KEY`, `LINEAR_TEAM_ID`, `LINEAR_PROJECT_ID`, and `COST_APP_PUBLIC_URL` (deployed frontend origin, used to build deep links in issues). Resolve the Linear team/project IDs once with a GraphQL call — `query { teams { nodes { id name } } }` and `query { projects { nodes { id name } } }` — then paste the IDs into `.env`.
```

- [ ] **Step 6: Commit**

```bash
git add backend/tcgai_backend/cost_management/issue_trackers.py backend/tcgai_backend/cost_management/tests.py CLAUDE.md
git commit -m "Add LinearIssueTracker adapter; document investigation env vars"
```

---

## Task 4: `flag_chat` endpoint — happy path

**Files:**
- Create: `backend/tcgai_backend/cost_management/investigation_views.py`
- Modify: `backend/tcgai_backend/cost_management/urls.py`
- Test: `backend/tcgai_backend/cost_management/tests.py`

**Interfaces:**
- Consumes: `Chat`, `Message` models; `github_tracker`, `linear_tracker`, `IssueTrackerError`, `IssueRef` from Tasks 2–3; `settings.GITHUB_TRIGGER_LABEL`, `settings.COST_APP_PUBLIC_URL`.
- Produces:
  - `POST /api/cost/flag_chat/` — body `{"chat_id": str, "reason": str}`, session-authenticated.
  - On full success: `200 {"investigation_status": "flagged", "github_issue_url": str, "linear_issue_url": str, "flag_error": str}`.
  - Side effects: creates a GitHub issue, adds `settings.GITHUB_TRIGGER_LABEL`, creates a Linear issue whose description begins `GitHub issue: <url>`, posts a GitHub comment `Linked Linear issue: <url>`, and persists all nine investigation fields on the `Chat`.
  - Private helpers importable for reuse in Task 5: `_missing_settings() -> list[str]`, `_build_title(chat_id, reason) -> str`, `_build_issue_body(chat, messages, reason, username, flag_time) -> str`, `_persist_flag(chat, reason, username, flag_time, gh_ref, linear_ref_or_none, flag_error) -> None`.

- [ ] **Step 1: Write the failing happy-path test**

Append to `cost_management/tests.py`:

```python
INVESTIGATION_ENV = dict(
    GITHUB_TOKEN="tok",
    GITHUB_ISSUE_REPO="acme/widgets",
    GITHUB_TRIGGER_LABEL="agent:queued",
    LINEAR_API_KEY="lin_key",
    LINEAR_TEAM_ID="team-123",
    LINEAR_PROJECT_ID="proj-456",
    COST_APP_PUBLIC_URL="https://costapp.example.com",
)


@override_settings(**INVESTIGATION_ENV)
class FlagChatHappyPathTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reviewer", password="pw")
        self.client.force_login(self.user)
        self.chat = Chat.objects.create(chat_id="conv-flag-1", model="claude-haiku-4-5",
                                        intent="return_request", tokens_in=100, tokens_out=50)
        Message.objects.create(
            chat=self.chat, content="i want to return my order",
            llm_formatted_message="{}", returned_content="Sure, I can help with that.",
            llm_formatted_returned_message="{}", tokens_in=100, tokens_out=50,
            model="claude-haiku-4-5",
        )

    def _post(self, body=None):
        return self.client.post(
            "/api/cost/flag_chat/",
            data=json.dumps({"chat_id": "conv-flag-1", "reason": "Bot gave a wrong refund policy"}
                            if body is None else body),
            content_type="application/json",
        )

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_flag_creates_both_issues_and_persists(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef
        mock_gh.create_issue.return_value = IssueRef(
            id="I_1", number=7, url="https://github.com/acme/widgets/issues/7")
        mock_linear.create_issue.return_value = IssueRef(
            id="lin-uuid", number=None, url="https://linear.app/pm/issue/SHO-9")

        response = self._post()

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["investigation_status"], "flagged")
        self.assertEqual(data["github_issue_url"], "https://github.com/acme/widgets/issues/7")
        self.assertEqual(data["linear_issue_url"], "https://linear.app/pm/issue/SHO-9")
        self.assertEqual(data["flag_error"], "")

        chat = Chat.objects.get(chat_id="conv-flag-1")
        self.assertEqual(chat.investigation_status, "flagged")
        self.assertEqual(chat.flag_reason, "Bot gave a wrong refund policy")
        self.assertEqual(chat.flagged_by, "reviewer")
        self.assertIsNotNone(chat.flagged_at)
        self.assertEqual(chat.github_issue_number, 7)
        self.assertEqual(chat.github_issue_url, "https://github.com/acme/widgets/issues/7")
        self.assertEqual(chat.linear_issue_id, "lin-uuid")
        self.assertEqual(chat.linear_issue_url, "https://linear.app/pm/issue/SHO-9")
        self.assertEqual(chat.flag_error, "")

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_flag_issue_body_has_reason_metadata_and_transcript(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef
        mock_gh.create_issue.return_value = IssueRef(id="I_1", number=7, url="https://gh/7")
        mock_linear.create_issue.return_value = IssueRef(id="lin", number=None, url="https://lin/9")

        self._post()

        gh_title, gh_body = mock_gh.create_issue.call_args[0][0], mock_gh.create_issue.call_args[0][1]
        self.assertIn("conv-flag-1", gh_title)
        self.assertIn("Bot gave a wrong refund policy", gh_body)
        self.assertIn("## Flag reason", gh_body)
        self.assertIn("## Chat metadata", gh_body)
        self.assertIn("return_request", gh_body)
        self.assertIn("https://costapp.example.com/chats?chat=conv-flag-1", gh_body)
        self.assertIn("## Transcript", gh_body)
        self.assertIn("i want to return my order", gh_body)
        self.assertIn("Sure, I can help with that.", gh_body)

        # GitHub gets the trigger label; Linear description carries the GH url;
        # GitHub gets a back-link comment.
        mock_gh.add_label.assert_called_once()
        self.assertEqual(mock_gh.add_label.call_args[0][1], "agent:queued")
        linear_body = mock_linear.create_issue.call_args[0][1]
        self.assertIn("GitHub issue: https://gh/7", linear_body)
        mock_gh.add_comment.assert_called_once()
        self.assertIn("https://lin/9", mock_gh.add_comment.call_args[0][1])
```

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `python manage.py test cost_management.tests.FlagChatHappyPathTests`
Expected: FAIL — `404` because `/api/cost/flag_chat/` is not routed yet (and the view module does not exist).

- [ ] **Step 3: Create `investigation_views.py`**

Create `backend/tcgai_backend/cost_management/investigation_views.py`:

```python
import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .issue_trackers import IssueRef, IssueTrackerError, github_tracker, linear_tracker
from .models import Chat, Message

logger = logging.getLogger(__name__)

REQUIRED_SETTINGS = [
    "GITHUB_TOKEN",
    "GITHUB_ISSUE_REPO",
    "LINEAR_API_KEY",
    "LINEAR_TEAM_ID",
    "LINEAR_PROJECT_ID",
    "COST_APP_PUBLIC_URL",
]


def _missing_settings():
    return [name for name in REQUIRED_SETTINGS if not getattr(settings, name, "")]


def _build_title(chat_id, reason):
    flat = " ".join(reason.split())
    excerpt = flat[:60] + ("…" if len(flat) > 60 else "")
    return f"Investigate chat {chat_id}: {excerpt}"


def _build_issue_body(chat, messages, reason, username, flag_time):
    lines = [
        "## Flag reason",
        reason,
        "",
        "## Chat metadata",
        f"- chat_id: {chat.chat_id}",
        f"- intent: {chat.intent}",
        f"- eval score: {chat.evaluation_score if chat.evaluation_score is not None else 'not evaluated'}",
        f"- tokens in / out: {chat.tokens_in} / {chat.tokens_out}",
        f"- model: {chat.model}",
        f"- first seen: {chat.timestamp.isoformat() if chat.timestamp else 'unknown'}",
        f"- flagged by: {username} at {flag_time.isoformat()}",
        f"- Cost app: {settings.COST_APP_PUBLIC_URL}/chats?chat={chat.chat_id}",
        "",
        "## Transcript",
    ]
    for msg in messages:
        user_text = msg.content.strip() if (msg.content and msg.content.strip()) else "*(no user text recorded)*"
        lines.append(f"**User:** {user_text}")
        lines.append(f"**Assistant:** {msg.returned_content}")
        lines.append("")
    return "\n".join(lines)


def _persist_flag(chat, reason, username, flag_time, gh_ref, linear_ref, flag_error):
    with transaction.atomic():
        locked = Chat.objects.select_for_update().get(pk=chat.pk)
        locked.investigation_status = "flagged"
        locked.flag_reason = reason
        if not locked.flagged_at:
            locked.flagged_at = flag_time
            locked.flagged_by = username
        locked.github_issue_number = gh_ref.number
        locked.github_issue_url = gh_ref.url
        if linear_ref is not None:
            locked.linear_issue_id = linear_ref.id
            locked.linear_issue_url = linear_ref.url
        locked.flag_error = flag_error
        locked.save(update_fields=[
            "investigation_status", "flag_reason", "flagged_at", "flagged_by",
            "github_issue_number", "github_issue_url", "linear_issue_id",
            "linear_issue_url", "flag_error",
        ])


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def flag_chat(request):
    missing = _missing_settings()
    if missing:
        return JsonResponse(
            {"error": "investigation integration not configured", "missing": missing},
            status=503,
        )

    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    chat_id = data.get("chat_id")
    reason = (data.get("reason") or "").strip()

    if not reason:
        return JsonResponse({"error": "reason is required"}, status=400)

    try:
        chat = Chat.objects.get(chat_id=chat_id)
    except Chat.DoesNotExist:
        return JsonResponse({"error": "Chat not found"}, status=404)

    # Linear-retry branch: GitHub issue exists but Linear never got created.
    if chat.github_issue_number and not chat.linear_issue_id:
        return _create_linear_only(chat)

    if chat.investigation_status in ("flagged", "resolved"):
        return JsonResponse({"error": "chat already flagged"}, status=409)

    messages = list(Message.objects.filter(chat=chat).order_by("timestamp"))
    username = request.user.get_username()
    flag_time = now()
    title = _build_title(chat_id, reason)
    body = _build_issue_body(chat, messages, reason, username, flag_time)

    soft_errors = []

    try:
        gh_ref = github_tracker.create_issue(title, body)
    except IssueTrackerError as exc:
        logger.error("[investigation] chat=%s github create_issue failed: %s", chat_id, exc)
        return JsonResponse(
            {"error": "failed to create GitHub issue", "detail": exc.detail}, status=502
        )
    logger.info("[investigation] chat=%s github issue #%s created", chat_id, gh_ref.number)

    try:
        github_tracker.add_label(gh_ref, settings.GITHUB_TRIGGER_LABEL)
    except IssueTrackerError as exc:
        logger.warning("[investigation] chat=%s github add_label failed: %s", chat_id, exc)
        soft_errors.append(f"trigger label failed: {exc.detail}")

    linear_body = f"GitHub issue: {gh_ref.url}\n\n{body}"
    try:
        linear_ref = linear_tracker.create_issue(title, linear_body)
    except IssueTrackerError as exc:
        logger.error("[investigation] chat=%s linear create_issue failed: %s", chat_id, exc)
        _persist_flag(
            chat, reason, username, flag_time, gh_ref, None,
            "; ".join(soft_errors + [f"linear create failed: {exc.detail}"]),
        )
        return JsonResponse(
            {"investigation_status": "flagged", "github_issue_url": gh_ref.url,
             "linear_error": exc.detail},
            status=200,
        )
    logger.info("[investigation] chat=%s linear issue %s created", chat_id, linear_ref.id)

    try:
        github_tracker.add_comment(gh_ref, f"Linked Linear issue: {linear_ref.url}")
    except IssueTrackerError as exc:
        logger.warning("[investigation] chat=%s github add_comment failed: %s", chat_id, exc)
        soft_errors.append(f"back-link comment failed: {exc.detail}")

    flag_error = "; ".join(soft_errors)
    _persist_flag(chat, reason, username, flag_time, gh_ref, linear_ref, flag_error)

    return JsonResponse(
        {
            "investigation_status": "flagged",
            "github_issue_url": gh_ref.url,
            "linear_issue_url": linear_ref.url,
            "flag_error": flag_error,
        },
        status=200,
    )


def _create_linear_only(chat):
    messages = list(Message.objects.filter(chat=chat).order_by("timestamp"))
    flag_time = chat.flagged_at or now()
    title = _build_title(chat.chat_id, chat.flag_reason or "flagged for investigation")
    body = _build_issue_body(
        chat, messages, chat.flag_reason or "(no reason recorded)", chat.flagged_by or "unknown", flag_time
    )
    linear_body = f"GitHub issue: {chat.github_issue_url}\n\n{body}"
    try:
        linear_ref = linear_tracker.create_issue(title, linear_body)
    except IssueTrackerError as exc:
        logger.error("[investigation] chat=%s linear retry failed: %s", chat.chat_id, exc)
        return JsonResponse(
            {"investigation_status": "flagged", "linear_error": exc.detail}, status=200
        )

    chat.linear_issue_id = linear_ref.id
    chat.linear_issue_url = linear_ref.url
    chat.flag_error = ""
    chat.save(update_fields=["linear_issue_id", "linear_issue_url", "flag_error"])

    try:
        github_tracker.add_comment(
            IssueRef(id="", number=chat.github_issue_number, url=chat.github_issue_url),
            f"Linked Linear issue: {linear_ref.url}",
        )
    except IssueTrackerError as exc:
        logger.warning("[investigation] chat=%s github add_comment (retry) failed: %s", chat.chat_id, exc)

    logger.info("[investigation] chat=%s linear issue %s created (retry)", chat.chat_id, linear_ref.id)
    return JsonResponse(
        {"investigation_status": "flagged", "linear_issue_url": linear_ref.url, "flag_error": ""},
        status=200,
    )
```

- [ ] **Step 4: Wire the route**

In `cost_management/urls.py`, add the import and the path:

```python
from django.urls import path
from . import views
from . import investigation_views

urlpatterns = [
    # ... existing paths unchanged ...
    path('flag_chat/', investigation_views.flag_chat, name='flag_chat'),
]
```

(Add `path('flag_chat/', ...)` as the last entry in the list.)

- [ ] **Step 5: Run the tests, confirm they pass**

Run: `python manage.py test cost_management.tests.FlagChatHappyPathTests`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/tcgai_backend/cost_management/investigation_views.py backend/tcgai_backend/cost_management/urls.py backend/tcgai_backend/cost_management/tests.py
git commit -m "Add flag_chat endpoint (happy path): create + cross-link GitHub and Linear issues"
```

---

## Task 5: `flag_chat` — validation, preflight, partial-failure, idempotency

**Files:**
- Modify: `backend/tcgai_backend/cost_management/investigation_views.py` (only if a test reveals a gap — the Task 4 implementation already covers most branches; this task is primarily its test suite plus any fixes)
- Test: `backend/tcgai_backend/cost_management/tests.py`

**Interfaces:**
- Consumes: everything from Task 4.
- Produces (response contract this task locks in):
  - `503 {"error": "investigation integration not configured", "missing": [...]}` when any required setting is empty.
  - `400 {"error": "reason is required"}` for blank/whitespace reason; `400 {"error": "invalid JSON body"}` for non-JSON.
  - `404 {"error": "Chat not found"}` for unknown `chat_id`.
  - `409 {"error": "chat already flagged"}` when already `flagged`/`resolved` and a `linear_issue_id` is set.
  - `502 {"error": "failed to create GitHub issue", "detail": ...}` when GitHub `create_issue` raises — **nothing persisted**.
  - `200` with `flag_error` populated when `add_label` or `add_comment` raises (chat still `flagged`).
  - `200` with `linear_error` when Linear `create_issue` raises (GitHub fields persisted, status `flagged`).
  - Linear-retry: a second call on a chat that is `flagged` with an empty `linear_issue_id` calls **only** `linear_tracker.create_issue` (not `github_tracker.create_issue`) and fills the Linear fields.
  - Sequential double-submit: the second call returns `409` and `github_tracker.create_issue` was called exactly once across both.

- [ ] **Step 1: Write the failing tests**

Append to `cost_management/tests.py`:

```python
@override_settings(**INVESTIGATION_ENV)
class FlagChatValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reviewer", password="pw")
        self.client.force_login(self.user)
        self.chat = Chat.objects.create(chat_id="conv-v", model="claude-haiku-4-5")

    def _post(self, body):
        return self.client.post("/api/cost/flag_chat/", data=json.dumps(body),
                                content_type="application/json")

    @patch("cost_management.investigation_views.github_tracker")
    def test_blank_reason_is_400_and_calls_no_tracker(self, mock_gh):
        resp = self._post({"chat_id": "conv-v", "reason": "   "})
        self.assertEqual(resp.status_code, 400)
        mock_gh.create_issue.assert_not_called()

    @patch("cost_management.investigation_views.github_tracker")
    def test_unknown_chat_is_404(self, mock_gh):
        resp = self._post({"chat_id": "nope", "reason": "something"})
        self.assertEqual(resp.status_code, 404)
        mock_gh.create_issue.assert_not_called()

    @patch("cost_management.investigation_views.github_tracker")
    def test_already_flagged_with_linear_id_is_409(self, mock_gh):
        Chat.objects.filter(pk="conv-v").update(
            investigation_status="flagged", github_issue_number=3,
            github_issue_url="https://gh/3", linear_issue_id="lin-x",
            linear_issue_url="https://lin/x")
        resp = self._post({"chat_id": "conv-v", "reason": "again"})
        self.assertEqual(resp.status_code, 409)
        mock_gh.create_issue.assert_not_called()

    @override_settings(LINEAR_TEAM_ID="")
    @patch("cost_management.investigation_views.github_tracker")
    def test_missing_setting_is_503_with_missing_list(self, mock_gh):
        resp = self._post({"chat_id": "conv-v", "reason": "x"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("LINEAR_TEAM_ID", resp.json()["missing"])
        mock_gh.create_issue.assert_not_called()


@override_settings(**INVESTIGATION_ENV)
class FlagChatPartialFailureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reviewer", password="pw")
        self.client.force_login(self.user)
        self.chat = Chat.objects.create(chat_id="conv-p", model="claude-haiku-4-5")
        Message.objects.create(chat=self.chat, content="hi", llm_formatted_message="{}",
                               returned_content="hello", llm_formatted_returned_message="{}",
                               tokens_in=1, tokens_out=1, model="claude-haiku-4-5")

    def _post(self):
        return self.client.post(
            "/api/cost/flag_chat/",
            data=json.dumps({"chat_id": "conv-p", "reason": "bad answer"}),
            content_type="application/json",
        )

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_github_create_failure_is_502_and_persists_nothing(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueTrackerError
        mock_gh.create_issue.side_effect = IssueTrackerError("github", "create_issue", 500, "boom")

        resp = self._post()

        self.assertEqual(resp.status_code, 502)
        mock_linear.create_issue.assert_not_called()
        chat = Chat.objects.get(pk="conv-p")
        self.assertEqual(chat.investigation_status, "unflagged")
        self.assertIsNone(chat.github_issue_number)

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_label_failure_is_soft_and_persists_flag(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef, IssueTrackerError
        mock_gh.create_issue.return_value = IssueRef(id="I", number=5, url="https://gh/5")
        mock_gh.add_label.side_effect = IssueTrackerError("github", "add_label", 422, "no label")
        mock_linear.create_issue.return_value = IssueRef(id="lin", number=None, url="https://lin/5")

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        chat = Chat.objects.get(pk="conv-p")
        self.assertEqual(chat.investigation_status, "flagged")
        self.assertIn("trigger label", chat.flag_error)

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_linear_failure_persists_github_and_returns_linear_error(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef, IssueTrackerError
        mock_gh.create_issue.return_value = IssueRef(id="I", number=6, url="https://gh/6")
        mock_linear.create_issue.side_effect = IssueTrackerError("linear", "create_issue", 400, "bad project")

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        self.assertIn("linear_error", resp.json())
        chat = Chat.objects.get(pk="conv-p")
        self.assertEqual(chat.investigation_status, "flagged")
        self.assertEqual(chat.github_issue_number, 6)
        self.assertEqual(chat.linear_issue_id, "")
        self.assertIn("linear create failed", chat.flag_error)

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_comment_failure_is_soft(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef, IssueTrackerError
        mock_gh.create_issue.return_value = IssueRef(id="I", number=8, url="https://gh/8")
        mock_gh.add_comment.side_effect = IssueTrackerError("github", "add_comment", 500, "oops")
        mock_linear.create_issue.return_value = IssueRef(id="lin", number=None, url="https://lin/8")

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        chat = Chat.objects.get(pk="conv-p")
        self.assertEqual(chat.investigation_status, "flagged")
        self.assertEqual(chat.linear_issue_id, "lin")
        self.assertIn("back-link comment", chat.flag_error)

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_linear_retry_branch_only_calls_linear(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef
        Chat.objects.filter(pk="conv-p").update(
            investigation_status="flagged", flag_reason="bad answer",
            github_issue_number=9, github_issue_url="https://gh/9", linear_issue_id="")
        mock_linear.create_issue.return_value = IssueRef(id="lin-retry", number=None, url="https://lin/9")

        resp = self._post()

        self.assertEqual(resp.status_code, 200)
        mock_gh.create_issue.assert_not_called()
        mock_linear.create_issue.assert_called_once()
        chat = Chat.objects.get(pk="conv-p")
        self.assertEqual(chat.linear_issue_id, "lin-retry")
        self.assertEqual(chat.linear_issue_url, "https://lin/9")
        self.assertEqual(chat.flag_error, "")

    @patch("cost_management.investigation_views.linear_tracker")
    @patch("cost_management.investigation_views.github_tracker")
    def test_sequential_double_submit_creates_one_github_issue(self, mock_gh, mock_linear):
        from cost_management.issue_trackers import IssueRef
        mock_gh.create_issue.return_value = IssueRef(id="I", number=10, url="https://gh/10")
        mock_linear.create_issue.return_value = IssueRef(id="lin", number=None, url="https://lin/10")

        first = self._post()
        second = self._post()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(mock_gh.create_issue.call_count, 1)
```

- [ ] **Step 2: Run the tests, confirm status**

Run: `python manage.py test cost_management.tests.FlagChatValidationTests cost_management.tests.FlagChatPartialFailureTests`
Expected: Most PASS immediately (Task 4's implementation already handles these branches). If any FAIL, note which branch and continue to Step 3.

- [ ] **Step 3: Fix any gaps**

Only if a test failed: adjust `investigation_views.py` to satisfy the failing assertion, keeping every other test green. Likely-touch points:
- JSON parse guard already returns `400 {"error": "invalid JSON body"}`.
- `_missing_settings()` ordering — the `missing` list follows `REQUIRED_SETTINGS` order; the test only checks membership, so no change needed.
- If `test_github_create_failure_is_502_and_persists_nothing` fails because the chat row changed: ensure `flag_chat` does not call `_persist_flag` before the GitHub `create_issue` succeeds (it does not, in the Task 4 code — verify no stray `chat.save()`).

Re-run the command from Step 2 until PASS.

- [ ] **Step 4: Run the full suite**

Run: `python manage.py test cost_management`
Expected: PASS (all tasks' tests green).

- [ ] **Step 5: Commit**

```bash
git add backend/tcgai_backend/cost_management/investigation_views.py backend/tcgai_backend/cost_management/tests.py
git commit -m "Lock flag_chat validation, preflight, partial-failure and retry behaviour with tests"
```

---

## Task 6: Frontend — Investigation column + flag modal + deep link

**Files:**
- Create: `frontend/src/chatSummary/FlagChatModal.jsx`
- Modify: `frontend/src/chatSummary/ChatSummaryView.jsx`
- Modify: `frontend/src/chatSummary/ChatSummaryView.css`

**Interfaces:**
- Consumes: `POST ${VITE_API_URL}/api/cost/flag_chat/` with body `{chat_id, reason}` → `200 {investigation_status, github_issue_url?, linear_issue_url?, flag_error}` or an error `{error}` with non-2xx status.
- Produces: no code consumed by other tasks (end of Phase 1).

- [ ] **Step 1: Create `FlagChatModal.jsx`**

Create `frontend/src/chatSummary/FlagChatModal.jsx`:

```jsx
import { useState } from "react";

function FlagChatModal({ chatId, initialReason = "", pending, error, onSubmit, onClose }) {
  const [reason, setReason] = useState(initialReason);

  return (
    <div className="modal-overlay">
      <div className="flag-modal">
        <div className="modal-header">
          <h2>Flag chat {chatId}</h2>
        </div>

        <div className="flag-modal-body">
          <label htmlFor="flag-reason">
            Why does this conversation need investigation?
          </label>
          <textarea
            id="flag-reason"
            className="flag-reason-input"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={6}
            placeholder="Describe what looks wrong…"
            disabled={pending}
          />
          {error && <div className="flag-error">{error}</div>}
        </div>

        <div className="modal-footer">
          <button
            className="close-modal-button flag-cancel"
            onClick={onClose}
            disabled={pending}
          >
            Cancel
          </button>
          <button
            className="close-modal-button"
            onClick={() => onSubmit(reason.trim())}
            disabled={pending || reason.trim() === ""}
          >
            {pending ? <span className="spinner" /> : "Flag & create issues"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default FlagChatModal;
```

- [ ] **Step 2: Wire the modal, column, and deep link into `ChatSummaryView.jsx`**

Make these edits to `frontend/src/chatSummary/ChatSummaryView.jsx`:

**2a.** Update the imports at the top:

```jsx
import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import "./ChatSummaryView.css";
import FlagChatModal from "./FlagChatModal";
```

**2b.** Inside `ChatSummaryView`, after the existing `const [chats, setChats] = useState([]);` line, add flag-modal state:

```jsx
  const [searchParams] = useSearchParams();
  const [flagState, setFlagState] = useState(null); // { chatId, pending, error }
```

**2c.** After the existing "Fetch chats" `useEffect`, add the deep-link effect:

```jsx
  // Deep link: /chats?chat=<id> auto-opens that chat's transcript modal
  useEffect(() => {
    const chatParam = searchParams.get("chat");
    if (chatParam) {
      openChatModal(chatParam);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);
```

**2d.** Add the flag handlers next to `evaluateAccuracy` (anywhere inside the component, before `return`):

```jsx
  const openFlagModal = (chatId) => {
    setFlagState({ chatId, pending: false, error: null });
  };

  const closeFlagModal = () => setFlagState(null);

  const submitFlag = async (reason) => {
    if (!reason) return;
    const chatId = flagState.chatId;
    setFlagState((s) => ({ ...s, pending: true, error: null }));

    try {
      const res = await fetch(`${API_URL}/api/cost/flag_chat/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ chat_id: chatId, reason }),
      });
      const data = await res.json();

      if (!res.ok) {
        setFlagState((s) => ({
          ...s,
          pending: false,
          error: data.error || `Request failed (${res.status})`,
        }));
        return;
      }

      setChats((prev) =>
        prev.map((c) =>
          c.chat_id === chatId
            ? {
                ...c,
                investigation_status: data.investigation_status || "flagged",
                github_issue_url: data.github_issue_url ?? c.github_issue_url,
                linear_issue_url: data.linear_issue_url ?? c.linear_issue_url,
                flag_error: data.flag_error ?? "",
              }
            : c
        )
      );
      setFlagState(null);
    } catch (err) {
      setFlagState((s) => ({ ...s, pending: false, error: String(err) }));
    }
  };

  const renderInvestigationCell = (chat) => {
    const status = chat.investigation_status || "unflagged";
    const links = (
      <span className="issue-links">
        {chat.github_issue_url && (
          <a href={chat.github_issue_url} target="_blank" rel="noopener noreferrer">
            GitHub ↗
          </a>
        )}
        {chat.linear_issue_url && (
          <a href={chat.linear_issue_url} target="_blank" rel="noopener noreferrer">
            Linear ↗
          </a>
        )}
      </span>
    );

    if (status === "unflagged") {
      return (
        <button className="flag-button" onClick={() => openFlagModal(chat.chat_id)}>
          🚩 Flag
        </button>
      );
    }

    if (status === "resolved") {
      return (
        <span className="investigation-cell">
          <span className="badge badge-resolved">Resolved ✓</span>
          {links}
        </span>
      );
    }

    return (
      <span className="investigation-cell">
        <span className="badge badge-flagged">Flagged</span>
        {chat.flag_error ? (
          <button
            className="retry-link"
            title={chat.flag_error}
            onClick={() => openFlagModal(chat.chat_id)}
          >
            ⚠ Retry
          </button>
        ) : null}
        {links}
      </span>
    );
  };
```

**2e.** Add the column header — in the `<thead><tr>`, after `<th>Products</th>`:

```jsx
            <th>Investigation</th>
```

**2f.** Add the column cell — in the `<tbody>` row, after the Products `<td>` (the one rendering `chat.products_shown_count`):

```jsx
              <td>{renderInvestigationCell(chat)}</td>
```

**2g.** Render the modal — just before the closing `</div>` of the `chat-summary-container` (next to the existing `{selectedChatId && ( ... )}` modal block), add:

```jsx
      {flagState && (
        <FlagChatModal
          chatId={flagState.chatId}
          initialReason={
            chats.find((c) => c.chat_id === flagState.chatId)?.flag_reason || ""
          }
          pending={flagState.pending}
          error={flagState.error}
          onSubmit={submitFlag}
          onClose={closeFlagModal}
        />
      )}
```

- [ ] **Step 3: Add the CSS**

Append to `frontend/src/chatSummary/ChatSummaryView.css`:

```css
/* --- Investigation flag column --- */
.flag-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  background-color: #8b7cf6;
  color: #d6d6d6;
  border: none;
  border-radius: 6px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.2s, transform 0.2s;
}

.flag-button:hover {
  background-color: #a69ffb;
  transform: scale(1.05);
}

.investigation-cell {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.badge {
  display: inline-block;
  padding: 3px 8px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.badge-flagged {
  color: #1e1e1e;
  background-color: #f0b429;
}

.badge-resolved {
  color: #1e1e1e;
  background-color: #6bd968;
}

.issue-links {
  display: inline-flex;
  gap: 8px;
}

.issue-links a {
  color: #b2a8ff;
  font-size: 12px;
  text-decoration: none;
}

.issue-links a:hover {
  text-decoration: underline;
}

.retry-link {
  background: transparent;
  color: #f0b429;
  border: 1px solid #f0b429;
  border-radius: 4px;
  font-size: 11px;
  padding: 2px 6px;
  cursor: pointer;
}

.flag-modal {
  width: 480px;
  max-width: 90%;
  background: #1e1e1e;
  border-radius: 16px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 0 25px rgba(0, 0, 0, 0.5);
}

.flag-modal-body {
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  color: #ddd;
}

.flag-reason-input {
  width: 100%;
  background: #111;
  color: #eee;
  border: 1px solid #333;
  border-radius: 8px;
  padding: 10px;
  font-family: inherit;
  font-size: 14px;
  resize: vertical;
}

.flag-error {
  color: #ff7a7a;
  font-size: 13px;
}

.flag-cancel {
  background: #3a3a3a;
}
```

- [ ] **Step 4: Lint and build**

Run (from `frontend/`): `npm run lint`
Expected: PASS, no errors in `ChatSummaryView.jsx` or `FlagChatModal.jsx`.

Run: `npm run build`
Expected: build succeeds.

- [ ] **Step 5: Manual verification checklist**

Start the backend (`python manage.py runserver` from `backend/tcgai_backend/`, with the seven investigation env vars set to real values or, for a dry run, use a fine-grained PAT against a scratch repo) and the frontend (`npm run dev` from `frontend/`). Log in, go to `/chats`, and confirm:

- [ ] Every row shows an **Investigation** column with a `🚩 Flag` button on `unflagged` chats.
- [ ] Clicking `🚩 Flag` opens the modal; `Flag & create issues` is disabled until the textarea is non-empty.
- [ ] Submitting with a reason shows the spinner, then the row changes to a `Flagged` badge with `GitHub ↗` / `Linear ↗` links that open the real issues in new tabs.
- [ ] The created GitHub issue body contains the reason, the metadata block, the `…/chats?chat=<id>` link, and the full transcript; it has the `agent:queued` label; it has a comment linking the Linear issue.
- [ ] The Linear issue description starts with `GitHub issue: <url>`.
- [ ] Visiting `/chats?chat=<some id>` directly auto-opens that chat's transcript modal.
- [ ] Simulate a Linear failure (temporarily set `LINEAR_API_KEY` to a bad value, restart backend, flag a fresh chat): the row shows `Flagged` plus `⚠ Retry`; hovering `⚠ Retry` shows the error text; clicking it and resubmitting (with the key fixed) fills in the `Linear ↗` link and clears the retry marker.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/chatSummary/FlagChatModal.jsx frontend/src/chatSummary/ChatSummaryView.jsx frontend/src/chatSummary/ChatSummaryView.css
git commit -m "Add Investigation column, flag modal, and ?chat= deep link to chat summary"
```

---

## Self-Review

**1. Spec coverage (P1-tagged items):**

| Spec item | Task |
|---|---|
| `Chat` investigation fields + migration 0009 (all 3 status values) | Task 1 |
| Register fields in `admin.py` | Task 1 |
| `get_chat_ids` serializes new fields | Task 1 (test only — `.values()` is automatic) |
| `issue_trackers.py` — `IssueTracker` shape, `IssueRef`, `IssueTrackerError` | Tasks 2–3 |
| `GitHubIssueTracker.create_issue` / `add_label` / `add_comment`, 10s timeout, non-2xx → error | Task 2 |
| `LinearIssueTracker.create_issue` (GraphQL `issueCreate`, raw auth header, `errors` → raise) | Task 3 |
| Module singletons `github_tracker` / `linear_tracker` | Tasks 2–3 |
| Settings: 7 P1 env constants | Task 2 |
| `flag_chat` preflight (503 + `missing`) | Tasks 4–5 |
| `flag_chat` validation (400 blank / 404 unknown / 409 already) | Tasks 4–5 |
| `flag_chat` Linear-retry branch | Tasks 4–5 |
| Issue title + body (reason + metadata + deep link + full transcript, empty-content fallback) | Task 4 |
| GitHub create → add trigger label → Linear create (body carries GH url) → GitHub back-link comment | Task 4 |
| Partial-failure rules (502 GitHub / soft label / 200 Linear-fail / soft comment) | Tasks 4–5 |
| Idempotency (`select_for_update` re-check; never a 2nd GitHub issue; sequential double-submit → 409) | Tasks 4–5 |
| Structured `[investigation]` logging on every external call | Task 4 |
| `flag_error` persisted + returned | Tasks 4–5 |
| URL route `flag_chat/` | Task 4 |
| Frontend: Investigation column, Flag button, `FlagChatModal`, `flagged` badge + links, `⚠ Retry` | Task 6 |
| Frontend: `?chat=` deep link | Task 6 |
| CSS: `.flag-button`, `.badge`, `.badge-flagged`, `.retry-link`, flag-modal | Task 6 |
| `CLAUDE.md` env-var docs + Linear ID lookup note | Task 3 |
| No new Python packages / `requirements.txt` untouched | Global Constraints (no task touches it) |

No P1 gaps.

**2. Placeholder scan:** No "TBD"/"TODO"/"handle edge cases"/"similar to Task N". Every code step has literal code. Task 5 Step 3 is conditional-fix guidance, but it names the exact files, assertions, and touch points rather than deferring — acceptable because Task 4's implementation is complete and Task 5's tests are all concrete.

**3. Type consistency:**
- `IssueRef(id, number, url)` — defined Task 2, used identically in Tasks 3–5 (`number=None` for Linear).
- `IssueTrackerError(tracker, operation, status, detail)` + `.detail` attr — defined Task 2, raised/inspected consistently in Tasks 3–5.
- `github_tracker` / `linear_tracker` singleton names — defined Tasks 2–3, patched as `cost_management.investigation_views.github_tracker` / `linear_tracker` in Tasks 4–5 (the view imports the names into its own module namespace, so patching the view module's reference is correct).
- Helper names `_missing_settings`, `_build_title`, `_build_issue_body`, `_persist_flag`, `_create_linear_only` — defined and called consistently within Task 4; Task 5 references the same names.
- Response keys (`investigation_status`, `github_issue_url`, `linear_issue_url`, `flag_error`, `linear_error`, `error`, `missing`, `detail`) — consistent across Tasks 4–5 and consumed by the frontend in Task 6.
- Frontend: `flagState` shape `{ chatId, pending, error }` — consistent across handlers and the `<FlagChatModal>` props (`chatId`, `pending`, `error`, `onSubmit`, `onClose`, `initialReason`).

No inconsistencies found.
