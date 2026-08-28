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
