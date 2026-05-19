"""Contract test — FakeQueue conforms to QueuePort."""

from __future__ import annotations

import pytest

from openbot.application.ports.queue import QueuePort
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from tests._fakes.queue import FakeQueue


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeQueue(), QueuePort)


def _event(delivery_id: str = "d-1") -> UnifiedEvent:
    """Build a minimal UnifiedEvent for contract tests."""
    return UnifiedEvent(
        channel="github",
        delivery_id=delivery_id,
        kind=EventKind.PR_OPENED,
        repo="owner/repo",
        actor="user",
        actor_type=None,
        issue_number=None,
        pr_number=1,
        comment_body=None,
        installation_id=None,
        event_seq=0,
        raw={},
    )


@pytest.mark.asyncio
async def test_enqueue_returns_unique_id() -> None:
    q = FakeQueue()
    a = await q.enqueue(_event("a"), feature=Feature.REVIEW, task_id="task-1")
    b = await q.enqueue(_event("b"), feature=Feature.TRIAGE, task_id="task-2")
    assert a != b
    assert len(q.calls) == 2
