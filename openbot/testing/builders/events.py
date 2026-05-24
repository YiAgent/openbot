"""UnifiedEvent factory functions for tests.

One builder per webhook kind. Each builder takes keyword-only args with
sensible defaults so tests state ONLY what they care about. Required
domain fields are populated; optional fields default to None or
deterministic values (delivery_id auto-generates a uuid4 if not given).
"""

from __future__ import annotations

import uuid
from typing import Any

from openbot.domain.events import EventKind, UnifiedEvent


def _delivery(delivery_id: str | None) -> str:
    return delivery_id or str(uuid.uuid4())


def build_issue_opened_event(
    *,
    repo: str = "owner/repo",
    issue_number: int = 1,
    sender: str = "octocat",
    body: str = "test issue body",
    title: str = "test issue title",
    delivery_id: str | None = None,
    installation_id: int = 100,
    event_seq: int = 0,
    clone_url: str | None = None,
) -> UnifiedEvent:
    """Build a deterministic issues.opened UnifiedEvent.

    Read openbot.domain.events.UnifiedEvent for the canonical field list;
    if a field is missing here, it's because no current test needs it —
    add a kwarg before depending on a default."""
    raw: dict[str, Any] = {
        "action": "opened",
        "issue": {"number": issue_number, "title": title, "body": body},
        "repository": {
            "full_name": repo,
            "clone_url": clone_url or f"https://github.com/{repo}.git",
        },
        "sender": {"login": sender, "type": "User"},
        "installation": {"id": installation_id},
    }
    return UnifiedEvent(
        channel="github",
        delivery_id=_delivery(delivery_id),
        kind=EventKind.ISSUE_OPENED,
        repo=repo,
        actor=sender,
        issue_number=issue_number,
        comment_body=body,
        actor_type="User",
        installation_id=installation_id,
        event_seq=event_seq,
        clone_url=clone_url,
        raw=raw,
    )


def build_pull_request_opened_event(
    *,
    repo: str = "owner/repo",
    pr_number: int = 1,
    sender: str = "octocat",
    title: str = "test PR",
    delivery_id: str | None = None,
    installation_id: int = 100,
    event_seq: int = 0,
    clone_url: str | None = None,
) -> UnifiedEvent:
    raw: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"number": pr_number, "title": title},
        "repository": {
            "full_name": repo,
            "clone_url": clone_url or f"https://github.com/{repo}.git",
        },
        "sender": {"login": sender, "type": "User"},
        "installation": {"id": installation_id},
    }
    return UnifiedEvent(
        channel="github",
        delivery_id=_delivery(delivery_id),
        kind=EventKind.PR_OPENED,
        repo=repo,
        actor=sender,
        pr_number=pr_number,
        actor_type="User",
        installation_id=installation_id,
        event_seq=event_seq,
        clone_url=clone_url,
        raw=raw,
    )


def build_issue_comment_command_event(
    *,
    repo: str = "owner/repo",
    issue_number: int = 1,
    sender: str = "octocat",
    command: str = "/fix",
    delivery_id: str | None = None,
    installation_id: int = 100,
    event_seq: int = 0,
) -> UnifiedEvent:
    raw: dict[str, Any] = {
        "action": "created",
        "issue": {"number": issue_number},
        "comment": {"body": command, "user": {"login": sender, "type": "User"}},
        "repository": {"full_name": repo},
        "sender": {"login": sender, "type": "User"},
        "installation": {"id": installation_id},
    }
    return UnifiedEvent(
        channel="github",
        delivery_id=_delivery(delivery_id),
        kind=EventKind.ISSUE_COMMENT_CREATED,
        repo=repo,
        actor=sender,
        issue_number=issue_number,
        comment_body=command,
        actor_type="User",
        installation_id=installation_id,
        event_seq=event_seq,
        raw=raw,
    )


__all__ = [
    "build_issue_comment_command_event",
    "build_issue_opened_event",
    "build_pull_request_opened_event",
]
