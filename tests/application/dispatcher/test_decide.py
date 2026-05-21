# tests/application/dispatcher/test_decide.py
"""decide_and_enqueue — isolated unit tests for unique code paths.

TaskSpec field assertions and label-extraction checks live in
``tests/e2e/test_decide_and_enqueue_demos.py`` (spec_v3_01-03) with full
FakeRedis+SQLite backing. This file tests the two paths that demos don't cover:
the queue=None in-process fallback and the never-raises contract for failing queues.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from openbot.dispatcher import decide_and_enqueue
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader

# Re-use the make_event helper from the middleware conftest
from tests.application.middleware.conftest import make_event


@pytest.mark.asyncio
async def test_decide_and_enqueue_falls_back_in_process_when_no_queue() -> None:
    """No queue provided → handler runs in-process instead of enqueuing."""
    event = make_event()
    from openbot.application.router import dispatch_for

    dispatch = dispatch_for(event)
    assert dispatch is not None
    called: list[str] = []

    async def fake_handler(ctx) -> None:
        called.append(ctx.event.delivery_id)

    dispatch = replace(dispatch, handler=fake_handler)

    await decide_and_enqueue(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=None,
        session_factory=None,
        redis=None,
    )

    assert called == [event.delivery_id]


@pytest.mark.asyncio
async def test_decide_and_enqueue_never_raises() -> None:
    """Any internal exception (e.g. Redis down) must be swallowed at the boundary."""
    event = make_event()
    from openbot.application.router import dispatch_for

    dispatch = dispatch_for(event)
    assert dispatch is not None

    class ExplodingQueue:
        async def enqueue(self, *_a: object, **_kw: object) -> str:
            return "0-0"

        async def enqueue_task_spec(self, _spec: object) -> str:
            raise RuntimeError("redis down")

    await decide_and_enqueue(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=ExplodingQueue(),
        session_factory=None,
        redis=None,
    )
