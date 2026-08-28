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

    def _post(self, url, payload, operation):
        try:
            return requests.post(
                url, headers=self._headers(), json=payload, timeout=HTTP_TIMEOUT
            )
        except requests.RequestException as exc:
            raise IssueTrackerError("github", operation, None, str(exc)[:500])

    def _check(self, resp, operation):
        if resp.status_code >= 300:
            raise IssueTrackerError("github", operation, resp.status_code, resp.text[:500])

    def create_issue(self, title, body, labels=None):
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        resp = self._post(self._repo_url("/issues"), payload, "create_issue")
        self._check(resp, "create_issue")
        try:
            data = resp.json()
        except ValueError:
            raise IssueTrackerError(
                "github", "create_issue", resp.status_code,
                "invalid JSON response: " + resp.text[:300],
            )
        return IssueRef(id=data["node_id"], number=data["number"], url=data["html_url"])

    def add_label(self, ref, label):
        resp = self._post(
            self._repo_url(f"/issues/{ref.number}/labels"),
            {"labels": [label]},
            "add_label",
        )
        self._check(resp, "add_label")

    def add_comment(self, ref, body):
        resp = self._post(
            self._repo_url(f"/issues/{ref.number}/comments"),
            {"body": body},
            "add_comment",
        )
        self._check(resp, "add_comment")


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
        try:
            resp = requests.post(
                LINEAR_API,
                headers=self._headers(),
                json={"query": self._MUTATION, "variables": variables},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise IssueTrackerError("linear", "create_issue", None, str(exc)[:500])
        if resp.status_code >= 300:
            raise IssueTrackerError("linear", "create_issue", resp.status_code, resp.text[:500])
        try:
            data = resp.json()
        except ValueError:
            raise IssueTrackerError(
                "linear", "create_issue", resp.status_code,
                "invalid JSON response: " + resp.text[:300],
            )
        if data.get("errors"):
            raise IssueTrackerError(
                "linear", "create_issue", resp.status_code, json.dumps(data["errors"])[:500]
            )
        issue = (((data or {}).get("data") or {}).get("issueCreate") or {}).get("issue")
        if not issue:
            raise IssueTrackerError(
                "linear", "create_issue", resp.status_code,
                "unexpected response: " + json.dumps(data)[:300],
            )
        return IssueRef(id=issue["id"], number=None, url=issue["url"])


github_tracker = GitHubIssueTracker()
linear_tracker = LinearIssueTracker()
