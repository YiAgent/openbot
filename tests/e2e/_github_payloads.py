"""Realistic GitHub webhook payload builders for e2e tests.

Each helper returns a ``(headers, body)`` tuple ready for ``TestClient.post``.
Bodies mirror the shape GitHub actually sends — only the fields we read
(``action``, ``issue``/``pull_request``, ``comment``, ``sender``,
``repository``, ``installation``) are populated, but the structure (nested
``repo`` under ``head``/``base``, etc.) matches the real wire format so
``GitHubAdapter.parse_event`` is exercised end-to-end.

Each helper signs the body with ``WEBHOOK_SECRET`` using the same HMAC-SHA256
scheme GitHub uses, so the FastAPI endpoint's signature gate is exercised on
the way in. Tests don't sign manually — they call these helpers.
"""

from __future__ import annotations

import hmac
import json
from hashlib import sha256
from typing import Any

# Single shared secret across the helpers + the ``client`` fixture in
# tests/e2e/test_webhook_events.py. Long enough that pydantic-settings
# doesn't reject it; the value itself is irrelevant beyond round-trip.
WEBHOOK_SECRET = "openbot-e2e-webhook-secret"

# Stable repo / installation / sender we reuse across helpers. Keeping these
# constant means task_id derivation (sha256 of channel|repo|delivery_id)
# only varies by delivery_id, which the test controls explicitly.
_REPO = "acme-corp/widgets"
_INSTALLATION_ID = 42_424_242
_HUMAN_SENDER = {"login": "alice", "id": 1001, "type": "User"}
_BOT_SENDER = {"login": "dependabot[bot]", "id": 49_699_333, "type": "Bot"}


def _sign(body: bytes, *, event: str, delivery_id: str) -> dict[str, str]:
    """Build the three headers GitHub sets on every webhook delivery."""
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, sha256).hexdigest()
    return {
        "x-hub-signature-256": f"sha256={digest}",
        "x-github-event": event,
        "x-github-delivery": delivery_id,
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/abc1234",
    }


def _encode(
    payload: dict[str, Any], *, event: str, delivery_id: str
) -> tuple[dict[str, str], bytes]:
    body = json.dumps(payload).encode()
    return _sign(body, event=event, delivery_id=delivery_id), body


# ───────────────────────── issues ─────────────────────────


def issue_opened(
    *, delivery_id: str = "iss-open-1", number: int = 7
) -> tuple[dict[str, str], bytes]:
    """`issues.opened` — triggers TRIAGE."""
    payload = {
        "action": "opened",
        "issue": {
            "number": number,
            "title": "Crash on startup when config is empty",
            "body": "Steps to reproduce: …",
            "user": _HUMAN_SENDER,
            "state": "open",
        },
        "repository": {"full_name": _REPO, "private": False},
        "sender": _HUMAN_SENDER,
        "installation": {"id": _INSTALLATION_ID},
    }
    return _encode(payload, event="issues", delivery_id=delivery_id)


def issue_assigned_to_bot(
    *, delivery_id: str = "iss-assign-bot-1", number: int = 11
) -> tuple[dict[str, str], bytes]:
    """`issues.assigned` with bot assignee — triggers FIX."""
    bot_assignee = {"login": "openbot[bot]", "id": 49_700_000, "type": "Bot"}
    payload = {
        "action": "assigned",
        "issue": {
            "number": number,
            "title": "Fix flaky test in scheduler",
            "user": _HUMAN_SENDER,
            "assignees": [bot_assignee],
            "state": "open",
        },
        "assignee": bot_assignee,
        "repository": {"full_name": _REPO, "private": False},
        "sender": _HUMAN_SENDER,  # human did the assigning
        "installation": {"id": _INSTALLATION_ID},
    }
    return _encode(payload, event="issues", delivery_id=delivery_id)


def issue_assigned_to_human(
    *, delivery_id: str = "iss-assign-human-1", number: int = 12
) -> tuple[dict[str, str], bytes]:
    """`issues.assigned` with human assignee — router should return None (ignored)."""
    human_assignee = {"login": "bob", "id": 1002, "type": "User"}
    payload = {
        "action": "assigned",
        "issue": {
            "number": number,
            "title": "Investigate user report",
            "user": _HUMAN_SENDER,
            "assignees": [human_assignee],
            "state": "open",
        },
        "assignee": human_assignee,
        "repository": {"full_name": _REPO, "private": False},
        "sender": _HUMAN_SENDER,
        "installation": {"id": _INSTALLATION_ID},
    }
    return _encode(payload, event="issues", delivery_id=delivery_id)


# ───────────────────────── pull_request ─────────────────────────


def pr_opened(*, delivery_id: str = "pr-open-1", number: int = 23) -> tuple[dict[str, str], bytes]:
    """`pull_request.opened` (same-repo) — triggers REVIEW."""
    payload = {
        "action": "opened",
        "pull_request": {
            "number": number,
            "title": "Add retry to webhook handler",
            "body": "Adds tenacity retry on 5xx.",
            "user": _HUMAN_SENDER,
            "state": "open",
            "draft": False,
            "head": {
                "ref": "feat/retry",
                "sha": "deadbeefcafebabe1234567890abcdef12345678",
                "repo": {"full_name": _REPO, "fork": False},
            },
            "base": {
                "ref": "main",
                "sha": "1234567890abcdef1234567890abcdef12345678",
                "repo": {"full_name": _REPO, "fork": False},
            },
        },
        "repository": {"full_name": _REPO, "private": False},
        "sender": _HUMAN_SENDER,
        "installation": {"id": _INSTALLATION_ID},
    }
    return _encode(payload, event="pull_request", delivery_id=delivery_id)


def pr_synchronize(
    *, delivery_id: str = "pr-sync-1", number: int = 23
) -> tuple[dict[str, str], bytes]:
    """`pull_request.synchronize` — also triggers REVIEW (rerun on push)."""
    payload = json.loads(pr_opened(delivery_id=delivery_id, number=number)[1])
    payload["action"] = "synchronize"
    payload["before"] = "1111111111111111111111111111111111111111"
    payload["after"] = "2222222222222222222222222222222222222222"
    return _encode(payload, event="pull_request", delivery_id=delivery_id)


# ───────────────────────── issue_comment ─────────────────────────


def issue_comment_on_issue_chat(
    *,
    delivery_id: str = "iss-comm-1",
    issue_number: int = 5,
    body: str = "@openbot what does this code do?",
) -> tuple[dict[str, str], bytes]:
    """`issue_comment.created` on a plain issue with @openbot prefix — CHAT."""
    payload = {
        "action": "created",
        "issue": {
            "number": issue_number,
            "title": "Question",
            "user": _HUMAN_SENDER,
            "state": "open",
            # No `pull_request` key — this is a plain issue.
        },
        "comment": {
            "id": 99_001,
            "user": _HUMAN_SENDER,
            "body": body,
        },
        "repository": {"full_name": _REPO, "private": False},
        "sender": _HUMAN_SENDER,
        "installation": {"id": _INSTALLATION_ID},
    }
    return _encode(payload, event="issue_comment", delivery_id=delivery_id)


def issue_comment_on_pr_chat(
    *, delivery_id: str = "iss-comm-pr-1", pr_number: int = 23
) -> tuple[dict[str, str], bytes]:
    """`issue_comment.created` on a PR with @openbot prefix.

    GitHub still emits ``X-GitHub-Event: issue_comment`` for PR comments;
    the only signal that it's a PR is ``issue.pull_request`` being set, and
    ``issue.number`` IS the PR number. ``parse_event`` swaps it into
    ``pr_number``.
    """
    payload = {
        "action": "created",
        "issue": {
            "number": pr_number,
            "title": "Add retry to webhook handler",
            "user": _HUMAN_SENDER,
            "state": "open",
            "pull_request": {
                "url": f"https://api.github.com/repos/{_REPO}/pulls/{pr_number}",
                "html_url": f"https://github.com/{_REPO}/pull/{pr_number}",
            },
        },
        "comment": {
            "id": 99_002,
            "user": _HUMAN_SENDER,
            "body": "@openbot summarize the diff",
        },
        "repository": {"full_name": _REPO, "private": False},
        "sender": _HUMAN_SENDER,
        "installation": {"id": _INSTALLATION_ID},
    }
    return _encode(payload, event="issue_comment", delivery_id=delivery_id)


def issue_comment_unrelated(
    *, delivery_id: str = "iss-comm-nopfx-1", issue_number: int = 5
) -> tuple[dict[str, str], bytes]:
    """`issue_comment.created` without `@openbot ` prefix — router ignores."""
    return issue_comment_on_issue_chat(
        delivery_id=delivery_id,
        issue_number=issue_number,
        body="ping anyone?",  # no @openbot prefix
    )


# ──────────────────── pull_request_review_comment ────────────────────


def pr_review_comment_chat(
    *, delivery_id: str = "pr-review-comm-1", pr_number: int = 23
) -> tuple[dict[str, str], bytes]:
    """`pull_request_review_comment.created` with @openbot — CHAT.

    Distinct event from issue_comment-on-PR: this is a code-line review
    comment with `pull_request` (not `issue`) at the top level.
    """
    payload = {
        "action": "created",
        "pull_request": {
            "number": pr_number,
            "title": "Add retry to webhook handler",
            "user": _HUMAN_SENDER,
            "state": "open",
            "head": {"repo": {"full_name": _REPO, "fork": False}},
            "base": {"repo": {"full_name": _REPO, "fork": False}},
        },
        "comment": {
            "id": 99_003,
            "user": _HUMAN_SENDER,
            "path": "openbot/webapp.py",
            "line": 224,
            "body": "@openbot explain this hunk",
        },
        "repository": {"full_name": _REPO, "private": False},
        "sender": _HUMAN_SENDER,
        "installation": {"id": _INSTALLATION_ID},
    }
    return _encode(payload, event="pull_request_review_comment", delivery_id=delivery_id)


# ───────────────────────── ping ─────────────────────────


def ping(*, delivery_id: str = "ping-1") -> tuple[dict[str, str], bytes]:
    """`ping` — the first delivery GitHub sends after App install.

    No ``action`` field; ``X-GitHub-Event: ping``. Should be accepted by
    the webhook (signature valid, JSON valid) and reported as ``ignored``
    by the router — we have no ping handler.
    """
    payload = {
        "zen": "Non-blocking is better than blocking.",
        "hook_id": 12_345_678,
        "hook": {"type": "App", "events": ["issues", "pull_request"]},
        "repository": {"full_name": _REPO},
        "sender": _HUMAN_SENDER,
        "installation": {"id": _INSTALLATION_ID},
    }
    return _encode(payload, event="ping", delivery_id=delivery_id)


# ───────────────────────── bot author / echo loop guard ─────────────────────────


def issue_opened_by_bot(
    *, delivery_id: str = "iss-open-bot-1", number: int = 8
) -> tuple[dict[str, str], bytes]:
    """`issues.opened` authored by a Bot — router must drop to avoid echo loops.

    Realistic scenario: dependabot opens an issue, or our own openbot[bot]
    posts a "scheduled job" comment that fires `issues.opened` on another
    automation. We never want to triage these.
    """
    payload = {
        "action": "opened",
        "issue": {
            "number": number,
            "title": "[Dependabot] Bump fastapi from 0.110 to 0.111",
            "user": _BOT_SENDER,
            "state": "open",
        },
        "repository": {"full_name": _REPO, "private": False},
        "sender": _BOT_SENDER,
        "installation": {"id": _INSTALLATION_ID},
    }
    return _encode(payload, event="issues", delivery_id=delivery_id)


# ───────────────────────── malformed payload ─────────────────────────


def malformed_json(*, delivery_id: str = "bad-json-1") -> tuple[dict[str, str], bytes]:
    """Valid HMAC over invalid JSON — adapter raises SignatureError → 401.

    Surfaces as 401 (not 400/500) because the adapter treats "passed HMAC
    but failed JSON" as a likely-attacker-probe pattern.
    """
    body = b'{"action": "opened", "this is broken'
    return _sign(body, event="issues", delivery_id=delivery_id), body


__all__ = [
    "WEBHOOK_SECRET",
    "issue_assigned_to_bot",
    "issue_assigned_to_human",
    "issue_comment_on_issue_chat",
    "issue_comment_on_pr_chat",
    "issue_comment_unrelated",
    "issue_opened",
    "issue_opened_by_bot",
    "malformed_json",
    "ping",
    "pr_opened",
    "pr_review_comment_chat",
    "pr_synchronize",
]
