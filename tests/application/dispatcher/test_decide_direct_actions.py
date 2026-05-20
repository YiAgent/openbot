# tests/application/dispatcher/test_decide_direct_actions.py
"""Integration tests: decide_and_enqueue() direct-action short-circuit (D11+D12)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from openbot.dispatcher import decide_and_enqueue
from openbot.domain.events import EventKind, UnifiedEvent
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests._fakes.queue import FakeQueue


def _pr_event(additions: int, deletions: int = 0) -> UnifiedEvent:
    """Build a PR_OPENED event with realistic raw payload."""
    return UnifiedEvent(
        channel="github",
        delivery_id="del-pr",
        kind=EventKind.PR_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        issue_number=None,
        pr_number=42,
        installation_id=100,
        comment_body=None,
        raw={
            "pull_request": {
                "number": 42,
                "additions": additions,
                "deletions": deletions,
                "changed_files": 10,
            }
        },
    )


def _issue_event(body: str | None, *, body_key_absent: bool = False) -> UnifiedEvent:
    """Build an ISSUE_OPENED event. body_key_absent=True omits the body key."""
    issue: dict = {"number": 7, "labels": []}
    if not body_key_absent:
        issue["body"] = body
    return UnifiedEvent(
        channel="github",
        delivery_id="del-issue",
        kind=EventKind.ISSUE_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        issue_number=7,
        pr_number=None,
        installation_id=100,
        comment_body=None,
        raw={"issue": issue},
    )


@pytest.mark.asyncio
async def test_empty_body_sends_reply_and_adds_label_no_enqueue() -> None:
    """Empty issue body → reply with needs-info message, add label, no TaskSpec."""
    from openbot.application.router import dispatch_for

    event = _issue_event(body="")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    adapter = FakeChannelAdapter()
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    # No TaskSpec enqueued
    assert len(queue.task_specs) == 0
    # A reply was sent
    assert len(adapter.replies) == 1
    # The needs-info label was applied
    assert len(adapter.labels_added) == 1
    assert "needs-info" in adapter.labels_added[0][1]


@pytest.mark.asyncio
async def test_body_present_continues_to_enqueue() -> None:
    """Non-empty body → no direct action, normal enqueue path."""
    from openbot.application.router import dispatch_for

    event = _issue_event(body="This is a detailed issue description about the auth bug.")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    adapter = FakeChannelAdapter()
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    assert len(queue.task_specs) == 1
    assert len(adapter.replies) == 0
    assert len(adapter.labels_added) == 0


@pytest.mark.asyncio
async def test_oversized_pr_sends_reply_no_enqueue() -> None:
    """PR with > 500 total line changes → split suggestion reply, no TaskSpec."""
    from openbot.application.router import dispatch_for

    event = _pr_event(additions=400, deletions=200)  # 600 total
    dispatch = dispatch_for(event)
    assert dispatch is not None

    adapter = FakeChannelAdapter()
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    assert len(queue.task_specs) == 0
    assert len(adapter.replies) == 1
    assert "600" in adapter.replies[0][1]


@pytest.mark.asyncio
async def test_normal_pr_size_continues_to_enqueue() -> None:
    """PR within threshold → no direct action."""
    from openbot.application.router import dispatch_for

    event = _pr_event(additions=100, deletions=50)  # 150 total, under 500
    dispatch = dispatch_for(event)
    assert dispatch is not None

    adapter = FakeChannelAdapter()
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    assert len(queue.task_specs) == 1
    assert len(adapter.replies) == 0


@pytest.mark.asyncio
async def test_direct_action_reply_failure_swallowed() -> None:
    """If adapter.reply() raises, decide_and_enqueue must NOT propagate."""
    from openbot.application.router import dispatch_for

    event = _issue_event(body="")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    adapter = FakeChannelAdapter()
    adapter.reply = AsyncMock(side_effect=RuntimeError("GitHub API down"))

    # Should not raise
    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=FakeQueue(),
        session_factory=None,
        redis=None,
    )
