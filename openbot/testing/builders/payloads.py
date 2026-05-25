"""GitHub webhook *raw payload* builders (dict, not UnifiedEvent).

Use when a test needs the bytes-level payload — for example, e2e
webhook posts that go through HMAC signing. UnifiedEvent builders are
in events.py; do not duplicate them here."""

from __future__ import annotations

from typing import Any


def build_issue_opened_payload(
    *,
    repo: str = "owner/repo",
    issue_number: int = 1,
    sender: str = "octocat",
    body: str = "test",
    title: str = "test",
    installation_id: int = 100,
) -> dict[str, Any]:
    """Return a minimal but schema-valid issues.opened webhook body."""
    owner, name = repo.split("/", 1)
    return {
        "action": "opened",
        "issue": {
            "number": issue_number,
            "title": title,
            "body": body,
            "user": {"login": sender},
        },
        "repository": {
            "full_name": repo,
            "name": name,
            "owner": {"login": owner},
        },
        "sender": {"login": sender},
        "installation": {"id": installation_id},
    }


def build_pull_request_opened_payload(
    *,
    repo: str = "owner/repo",
    pr_number: int = 1,
    sender: str = "octocat",
    title: str = "test PR",
    installation_id: int = 100,
) -> dict[str, Any]:
    owner, name = repo.split("/", 1)
    return {
        "action": "opened",
        "pull_request": {
            "number": pr_number,
            "title": title,
            "user": {"login": sender},
            "head": {"sha": "deadbeef" * 5, "ref": "feature"},
            "base": {"sha": "cafef00d" * 5, "ref": "main"},
        },
        "repository": {
            "full_name": repo,
            "name": name,
            "owner": {"login": owner},
        },
        "sender": {"login": sender},
        "installation": {"id": installation_id},
    }


__all__ = ["build_issue_opened_payload", "build_pull_request_opened_payload"]
