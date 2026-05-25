"""execute_handler — check-run finalization semantics.

These tests pin down the contract that `check_run_id` is *always* PATCHed
to a terminal state when one was supplied — otherwise the GitHub UI shows
the "OpenBot Analysis" check stuck in_progress forever.

Three crash modes must be covered:

1. handler returns normally           → conclusion="success"
2. handler raises Exception            → conclusion="failure" (do not re-raise)
3. handler raises CancelledError       → conclusion="cancelled" AND re-raise
   (CancelledError must propagate so the outer cancel scope completes)
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from openbot.application.dispatcher import execute_handler
from openbot.application.router import dispatch_for
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests.application.middleware.conftest import make_event


@pytest.mark.asyncio
async def test_check_run_marked_success_on_clean_handler() -> None:
    event = make_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None
    config = await FakeConfigLoader().load_for_repo(None, event)

    async def ok_handler(_ctx) -> None:
        return None

    dispatch = replace(dispatch, handler=ok_handler)
    adapter = FakeChannelAdapter()

    await execute_handler(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config=config,
        session_factory=None,
        redis=None,
        check_run_id=42,
    )

    assert len(adapter.check_run_updates) == 1
    update = adapter.check_run_updates[0]
    assert update["check_run_id"] == 42
    assert update["status"] == "completed"
    assert update["conclusion"] == "success"


@pytest.mark.asyncio
async def test_check_run_marked_failure_on_exception() -> None:
    event = make_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None
    config = await FakeConfigLoader().load_for_repo(None, event)

    async def crashing(_ctx) -> None:
        raise RuntimeError("boom")

    dispatch = replace(dispatch, handler=crashing)
    adapter = FakeChannelAdapter()

    # Must not raise — Exception path is fail-soft (existing contract).
    await execute_handler(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config=config,
        session_factory=None,
        redis=None,
        check_run_id=43,
    )

    assert len(adapter.check_run_updates) == 1
    update = adapter.check_run_updates[0]
    assert update["check_run_id"] == 43
    assert update["status"] == "completed"
    assert update["conclusion"] == "failure"


@pytest.mark.asyncio
async def test_check_run_marked_cancelled_and_reraises_on_cancellederror() -> None:
    """CancelledError must NOT be swallowed — the outer cancel scope needs it.

    But before re-raising, the check run must be PATCHed to ``cancelled`` so
    the PR UI doesn't show a stuck "in progress" check after a worker
    shutdown / SUPERSEDE / timeout.
    """
    event = make_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None
    config = await FakeConfigLoader().load_for_repo(None, event)

    async def cancelling(_ctx) -> None:
        raise asyncio.CancelledError()

    dispatch = replace(dispatch, handler=cancelling)
    adapter = FakeChannelAdapter()

    with pytest.raises(asyncio.CancelledError):
        await execute_handler(
            adapter=adapter,
            event=event,
            dispatch=dispatch,
            config=config,
            session_factory=None,
            redis=None,
            check_run_id=44,
        )

    assert len(adapter.check_run_updates) == 1
    update = adapter.check_run_updates[0]
    assert update["check_run_id"] == 44
    assert update["status"] == "completed"
    assert update["conclusion"] == "cancelled"


@pytest.mark.asyncio
async def test_no_check_run_update_when_id_absent() -> None:
    """When the webhook never created a check run, dispatcher must not invent one."""
    event = make_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None
    config = await FakeConfigLoader().load_for_repo(None, event)

    async def ok_handler(_ctx) -> None:
        return None

    dispatch = replace(dispatch, handler=ok_handler)
    adapter = FakeChannelAdapter()

    await execute_handler(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config=config,
        session_factory=None,
        redis=None,
        check_run_id=None,
    )

    assert adapter.check_run_updates == []
