# tests/application/dispatcher/test_decide.py
"""decide_and_enqueue — webhook async segment: preflight + TaskSpec v3 enqueue."""

from __future__ import annotations

from dataclasses import replace

import pytest

from openbot.dispatcher import decide_and_enqueue
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests._fakes.queue import FakeQueue

# Re-use the make_event helper from the middleware conftest
from tests.application.middleware.conftest import make_event


@pytest.mark.asyncio
async def test_decide_and_enqueue_builds_task_spec() -> None:
    """Happy path: no blocking middleware → TaskSpec v3 on FakeQueue."""
    event = make_event()
    from openbot.application.router import dispatch_for

    dispatch = dispatch_for(event)
    assert dispatch is not None
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.spec_version == 3
    assert spec.repo == event.repo
    assert spec.delivery_id == event.delivery_id
    assert spec.classifier_skipped is True
    assert spec.stages_to_run == []


@pytest.mark.asyncio
async def test_decide_and_enqueue_falls_back_in_process_when_no_queue() -> None:
    """No queue provided → handler runs in-process."""
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
    """Any internal exception must be swallowed."""
    event = make_event()
    from openbot.application.router import dispatch_for

    dispatch = dispatch_for(event)
    assert dispatch is not None

    class ExplodingQueue:
        async def enqueue(self, *a, **kw) -> str:
            return "0-0"

        async def enqueue_task_spec(self, spec) -> str:
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


@pytest.mark.asyncio
async def test_decide_and_enqueue_initial_labels_extracted() -> None:
    """Labels from raw GitHub payload reach the TaskSpec."""
    from openbot.application.router import dispatch_for
    from openbot.domain.events import EventKind, UnifiedEvent

    raw_with_labels = {
        "issue": {
            "number": 1,
            "labels": [{"name": "cancel-openbot"}, {"name": "bug"}],
        }
    }
    event = UnifiedEvent(
        channel="github",
        delivery_id="del-labels",
        kind=EventKind.ISSUE_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type=None,
        issue_number=1,
        pr_number=None,
        comment_body=None,
        installation_id=42,
        event_seq=0,
        raw=raw_with_labels,
    )
    dispatch = dispatch_for(event)
    assert dispatch is not None
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    assert len(queue.task_specs) == 1
    assert "cancel-openbot" in queue.task_specs[0].initial_labels
    assert "bug" in queue.task_specs[0].initial_labels
