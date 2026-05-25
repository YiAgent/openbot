"""Compat shim — provides ``make_event`` for legacy flat test files.

PR #87 deleted the original ``tests/application/middleware/conftest.py``
(its helpers were no longer used by restructured tests). Flat test files
from main that haven't been restructured yet import through this shim.
"""

from __future__ import annotations

from openbot.domain.events import EventKind, UnifiedEvent


def make_event(
    *,
    kind: EventKind = EventKind.ISSUE_OPENED,
    repo: str = "org/r",
    actor: str = "alice",
    actor_type: str | None = "User",
    issue_number: int | None = 7,
    pr_number: int | None = None,
    installation_id: int | None = 100,
    delivery_id: str = "deliv-1",
    comment_body: str | None = None,
    raw: dict | None = None,
) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id=delivery_id,
        kind=kind,
        repo=repo,
        actor=actor,
        actor_type=actor_type,
        issue_number=issue_number,
        pr_number=pr_number,
        installation_id=installation_id,
        comment_body=comment_body,
        raw=raw or {},
    )
