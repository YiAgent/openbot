"""Webhook payload builders for state-machine L2 tests.

Mirrors the pattern from ``tests/e2e/_github_payloads.py``. Each builder
returns ``bytes`` (the raw JSON body). ``sign`` adds the three headers
GitHub sets on every delivery. Tests control ``delivery`` IDs explicitly
so dedup and resource_key assertions are deterministic.
"""

from __future__ import annotations

import hmac
import json
from hashlib import sha256
from typing import Any

# Single secret wired to ``OPENBOT_GITHUB_WEBHOOK_SECRET`` in the sm fixture.
_SM_SECRET: str = "sm-l2-test-secret"

# Stable test repo — keeps resource keys short and readable in assertion output.
_REPO: str = "acme/testrepo"
_INSTALLATION_ID: int = 99

_HUMAN: dict[str, Any] = {"login": "alice", "id": 1001, "type": "User"}
_BOT_SENDER: dict[str, Any] = {"login": "openbot[bot]", "id": 99_000, "type": "Bot"}


def sign(
    body: bytes,
    *,
    event: str = "issues",
    delivery: str = "d-1",
) -> dict[str, str]:
    """Build the three webhook headers GitHub sends on every delivery."""
    digest = hmac.new(_SM_SECRET.encode(), body, sha256).hexdigest()
    return {
        "x-hub-signature-256": f"sha256={digest}",
        "x-github-event": event,
        "x-github-delivery": delivery,
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/sm-test",
    }


def issue_body(
    action: str,
    *,
    number: int | None = 42,
    actor_type: str = "User",
    omit_installation: bool = False,
) -> bytes:
    """Generic ``issues.*`` payload.

    Pass ``number=None`` to omit ``issue.number`` — triggers the
    ``dispatch_for`` guard that returns None → ``status=ignored``.
    Pass ``omit_installation=True`` to omit ``installation.id`` — same guard.
    """
    sender = _BOT_SENDER if actor_type == "Bot" else _HUMAN
    issue: dict[str, Any] = {"title": "test issue", "user": sender, "state": "open"}
    if number is not None:
        issue["number"] = number

    payload: dict[str, Any] = {
        "action": action,
        "issue": issue,
        "repository": {"full_name": _REPO, "private": False},
        "sender": sender,
    }
    if not omit_installation:
        payload["installation"] = {"id": _INSTALLATION_ID}
    return json.dumps(payload).encode()


def issue_assigned_body(*, number: int = 42, bot_assignee: bool = True) -> bytes:
    """``issues.assigned`` — bot or human assignee controls the router gate."""
    assignee: dict[str, Any] = (
        {"login": "openbot[bot]", "id": 99_000, "type": "Bot"}
        if bot_assignee
        else {"login": "bob", "id": 1002, "type": "User"}
    )
    payload: dict[str, Any] = {
        "action": "assigned",
        "issue": {"number": number, "title": "test issue", "user": _HUMAN, "state": "open"},
        "assignee": assignee,
        "repository": {"full_name": _REPO, "private": False},
        "sender": _HUMAN,
        "installation": {"id": _INSTALLATION_ID},
    }
    return json.dumps(payload).encode()


def pr_body(
    action: str,
    *,
    number: int = 7,
    head_sha: str = "abc123",
    draft: bool = False,
) -> bytes:
    """Generic ``pull_request.*`` payload."""
    payload: dict[str, Any] = {
        "action": action,
        "pull_request": {
            "number": number,
            "title": "test PR",
            "user": _HUMAN,
            "state": "open",
            "draft": draft,
            "head": {
                "ref": "feat/test",
                "sha": head_sha,
                "repo": {"full_name": _REPO, "fork": False},
            },
            "base": {
                "ref": "main",
                "sha": "0" * 40,
                "repo": {"full_name": _REPO, "fork": False},
            },
        },
        "repository": {"full_name": _REPO, "private": False},
        "sender": _HUMAN,
        "installation": {"id": _INSTALLATION_ID},
    }
    return json.dumps(payload).encode()


__all__ = [
    "_BOT_SENDER",
    "_HUMAN",
    "_INSTALLATION_ID",
    "_REPO",
    "_SM_SECRET",
    "issue_assigned_body",
    "issue_body",
    "pr_body",
    "sign",
]
