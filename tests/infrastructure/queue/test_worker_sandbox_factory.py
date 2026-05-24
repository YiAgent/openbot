"""Worker: sandbox_factory flows from consume_loop → execute_handler.

Confirms that the sandbox_factory kwarg introduced in Task 2 of the
evals-runtime-redesign plan reaches execute_handler unchanged.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from openbot.infrastructure.queue.worker import (
    STREAM_NAME,
    consume_loop,
    ensure_consumer_group,
)
from tests.application.middleware.conftest import make_event


def _make_spec():
    from openbot.application.router import dispatch_for
    from openbot.infrastructure.queue.task_spec import TaskSpec

    event = make_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None
    return TaskSpec.from_event_and_dispatch(event, dispatch)


@pytest.mark.asyncio
async def test_sandbox_factory_forwarded_to_execute_handler(monkeypatch) -> None:
    """sandbox_factory passed to consume_loop reaches execute_handler."""
    from tests._fakes.config_loader import FakeConfigLoader

    captured: dict = {}

    async def fake_execute_handler(**kw: object) -> None:
        captured.update(kw)

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        fake_execute_handler,
    )
    fake_loader = FakeConfigLoader()
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.load_for_repo",
        fake_loader.load_for_repo,
    )

    sentinel_factory = object()  # identity check — any non-None value

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)
    spec = _make_spec()
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_running_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=AsyncMock(),
        session_factory=None,
        consumer_name="test-sf",
        shutdown=shutdown,
        read_block_ms=50,
        sandbox_factory=sentinel_factory,
    )

    assert "sandbox_factory" in captured
    assert captured["sandbox_factory"] is sentinel_factory


@pytest.mark.asyncio
async def test_sandbox_factory_none_by_default(monkeypatch) -> None:
    """sandbox_factory defaults to None when omitted from consume_loop."""
    from tests._fakes.config_loader import FakeConfigLoader

    captured: dict = {}

    async def fake_execute_handler(**kw: object) -> None:
        captured.update(kw)

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        fake_execute_handler,
    )
    fake_loader = FakeConfigLoader()
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.load_for_repo",
        fake_loader.load_for_repo,
    )

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)
    spec = _make_spec()
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_running_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=AsyncMock(),
        session_factory=None,
        consumer_name="test-sf-none",
        shutdown=shutdown,
        read_block_ms=50,
        # sandbox_factory intentionally omitted
    )

    assert captured.get("sandbox_factory") is None
