"""Contract tests for QueuePort.

Each test runs twice: [fake] uses FakeQueue, [real] uses
RedisStreamQueue backed by fakeredis. Both must satisfy the same
assertions — drift in either direction fails CI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openbot.application.ports.queue import QueuePort
from openbot.domain.workflows import Feature
from openbot.infrastructure.queue.enqueue import RedisStreamQueue
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes import FakeQueue
from openbot.testing.inmemory.redis import build_inmemory_redis


@pytest.fixture(params=["fake", "real"])
async def queue(request: pytest.FixtureRequest) -> AsyncIterator[QueuePort]:
    if request.param == "fake":
        yield FakeQueue()
    else:
        async with build_inmemory_redis() as r:
            yield RedisStreamQueue(redis=r)


@pytest.mark.contract
class TestQueueContract:
    async def test_enqueue_returns_nonempty_id(self, queue: QueuePort) -> None:
        sid = await queue.enqueue(build_issue_opened_event(), feature=Feature.TRIAGE, task_id="t1")
        assert sid != ""

    async def test_enqueue_id_contains_dash(self, queue: QueuePort) -> None:
        """Redis stream IDs are '<ms>-<seq>'; FakeQueue mimics this."""
        sid = await queue.enqueue(build_issue_opened_event(), feature=Feature.TRIAGE, task_id="t1")
        assert "-" in sid

    async def test_enqueue_preserves_order(self, queue: QueuePort) -> None:
        """Stream IDs must be monotonically increasing."""
        sids = [
            await queue.enqueue(
                build_issue_opened_event(issue_number=i),
                feature=Feature.TRIAGE,
                task_id=f"t{i}",
            )
            for i in range(5)
        ]
        assert sids == sorted(sids)

    async def test_enqueue_distinct_ids_for_distinct_events(self, queue: QueuePort) -> None:
        sid1 = await queue.enqueue(build_issue_opened_event(), feature=Feature.TRIAGE, task_id="t1")
        sid2 = await queue.enqueue(build_issue_opened_event(), feature=Feature.TRIAGE, task_id="t2")
        assert sid1 != sid2
