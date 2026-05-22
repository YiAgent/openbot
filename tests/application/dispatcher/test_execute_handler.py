"""execute_handler — handler invocation without preflight."""

from __future__ import annotations

import pytest

from openbot.application.dispatcher import execute_handler
from openbot.application.middleware import PreflightContext
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests.application.middleware.conftest import make_event


@pytest.mark.asyncio
async def test_execute_handler_calls_handler() -> None:
    event = make_event()
    from openbot.application.router import dispatch_for

    dispatch = dispatch_for(event)
    assert dispatch is not None

    config = await FakeConfigLoader().load_for_repo(None, event)
    called: list[str] = []

    async def fake_handler(ctx) -> None:
        called.append(ctx.event.delivery_id)

    from dataclasses import replace

    dispatch = replace(dispatch, handler=fake_handler)

    await execute_handler(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config=config,
        session_factory=None,
        redis=None,
    )
    assert called == [event.delivery_id]


@pytest.mark.asyncio
async def test_execute_handler_passes_agent_checkpointer_to_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent_checkpointer kwarg is threaded into PreflightContext."""
    import openbot.application.dispatcher as dispatcher_mod

    sentinel = object()
    captured: list[PreflightContext] = []

    async def fake_run_with_sandbox(ctx: PreflightContext) -> None:
        captured.append(ctx)

    monkeypatch.setattr(dispatcher_mod, "_run_with_sandbox", fake_run_with_sandbox)

    event = make_event()
    from openbot.application.router import dispatch_for

    dispatch = dispatch_for(event)
    assert dispatch is not None
    config = await FakeConfigLoader().load_for_repo(None, event)

    await execute_handler(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config=config,
        session_factory=None,
        redis=None,
        agent_checkpointer=sentinel,  # type: ignore[arg-type]
    )

    assert len(captured) == 1
    assert captured[0].agent_checkpointer is sentinel


@pytest.mark.asyncio
async def test_execute_handler_agent_checkpointer_defaults_to_none() -> None:
    """agent_checkpointer defaults to None when not supplied."""
    import openbot.application.dispatcher as dispatcher_mod

    captured: list[PreflightContext] = []

    async def fake_run_with_sandbox(ctx: PreflightContext) -> None:
        captured.append(ctx)

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(dispatcher_mod, "_run_with_sandbox", fake_run_with_sandbox)

        event = make_event()
        from openbot.application.router import dispatch_for

        dispatch = dispatch_for(event)
        assert dispatch is not None
        config = await FakeConfigLoader().load_for_repo(None, event)

        await execute_handler(
            adapter=FakeChannelAdapter(),
            event=event,
            dispatch=dispatch,
            config=config,
            session_factory=None,
            redis=None,
        )

    assert len(captured) == 1
    assert captured[0].agent_checkpointer is None


@pytest.mark.asyncio
async def test_execute_handler_never_raises() -> None:
    event = make_event()
    from openbot.application.router import dispatch_for

    dispatch = dispatch_for(event)
    assert dispatch is not None
    config = await FakeConfigLoader().load_for_repo(None, event)

    async def crashing(_ctx) -> None:
        raise RuntimeError("boom")

    from dataclasses import replace

    dispatch = replace(dispatch, handler=crashing)

    # Must not raise
    await execute_handler(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config=config,
        session_factory=None,
        redis=None,
    )
