"""Contract test — FakeQueue conforms to QueuePort."""

from __future__ import annotations

import pytest

from openbot.application.ports.queue import QueuePort
from openbot.infrastructure.queue.payload import QueuePayload
from tests._fakes.queue import FakeQueue


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeQueue(), QueuePort)


def _payload(delivery_id: str) -> QueuePayload:
    """Build a minimal QueuePayload — keep kwargs aligned with the dataclass."""
    return QueuePayload(
        version=2,
        channel="github",
        delivery_id=delivery_id,
        kind="pull_request",
        repo="owner/repo",
        actor="user",
        actor_type=None,
        issue_number=None,
        pr_number=1,
        comment_body=None,
        installation_id=None,
        raw={},
        feature="review",
        task_id="task-1",
        enqueued_at="2024-01-01T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_enqueue_returns_unique_id() -> None:
    q = FakeQueue()
    a = await q.enqueue(_payload("a"))
    b = await q.enqueue(_payload("b"))
    assert a != b
    assert len(q.entries) == 2
