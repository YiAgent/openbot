"""Worker: v3 TaskSpec routing, W1 cancel quick-check, legacy fallback."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.infrastructure.queue.worker import (
    GROUP_NAME,
    STREAM_NAME,
    _is_v3_spec,
    consume_loop,
    ensure_consumer_group,
)

# Import the event-making helper from the middleware conftest.
from tests.application.middleware.conftest import make_event


def _make_spec(initial_labels: list[str] | None = None) -> TaskSpec:
    from openbot.application.router import dispatch_for

    event = make_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None
    return TaskSpec.from_event_and_dispatch(event, dispatch, initial_labels=initial_labels or [])


# ─── unit tests for _is_v3_spec ───────────────────────────────────────────────


def test_is_v3_spec_true() -> None:
    assert _is_v3_spec(json.dumps({"spec_version": 3})) is True


def test_is_v3_spec_false_for_v2_payload() -> None:
    assert _is_v3_spec(json.dumps({"version": 2, "task_id": "x"})) is False


def test_is_v3_spec_false_for_garbage() -> None:
    assert _is_v3_spec("not-json") is False
    assert _is_v3_spec(None) is False


def test_is_v3_spec_bytes_true() -> None:
    blob = json.dumps({"spec_version": 3}).encode()
    assert _is_v3_spec(blob) is True


# ─── integration tests against fakeredis ──────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_routes_v3_to_execute_handler(monkeypatch) -> None:
    """Worker calls execute_handler (not run_dispatch) for v3 specs."""
    from tests._fakes.config_loader import FakeConfigLoader

    handler_calls: list[str] = []

    async def fake_execute_handler(**kw: object) -> None:
        handler_calls.append(kw["event"].delivery_id)  # type: ignore[union-attr]

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
        consumer_name="test-v3",
        shutdown=shutdown,
        read_block_ms=50,
    )

    assert len(handler_calls) == 1
    assert handler_calls[0] == spec.delivery_id


@pytest.mark.asyncio
async def test_worker_v3_cancel_openbot_quick_exit(monkeypatch) -> None:
    """cancel-openbot in initial_labels → XACK immediately, no handler call."""
    from tests._fakes.config_loader import FakeConfigLoader

    handler_calls: list[str] = []

    async def fake_execute_handler(**_kw: object) -> None:
        handler_calls.append("called")

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
    spec = _make_spec(initial_labels=["cancel-openbot"])
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_running_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=AsyncMock(),
        session_factory=None,
        consumer_name="test-cancel",
        shutdown=shutdown,
        read_block_ms=50,
    )

    assert handler_calls == []
    pending = await redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_worker_legacy_v2_payload_still_works(monkeypatch) -> None:
    """v2 QueuePayload entries continue through the old path unchanged."""
    run_dispatch_calls: list[str] = []

    async def fake_run_dispatch(**kw: object) -> None:
        run_dispatch_calls.append(kw["event"].delivery_id)  # type: ignore[union-attr]

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.run_dispatch",
        fake_run_dispatch,
    )

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)

    from openbot.application.router import dispatch_for
    from openbot.infrastructure.queue.payload import QueuePayload

    event = make_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None
    payload = QueuePayload.from_event(
        event,
        feature=dispatch.feature,
        task_id=dispatch.task_id,
    )
    await redis.xadd(STREAM_NAME, {"json": payload.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_running_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=AsyncMock(),
        session_factory=None,
        consumer_name="test-legacy",
        shutdown=shutdown,
        read_block_ms=50,
    )

    assert len(run_dispatch_calls) == 1
    assert run_dispatch_calls[0] == event.delivery_id


@pytest.fixture(autouse=True)
def _no_anyio_marker():
    """asyncio mode=auto; fixture is a no-op kept as a clear marker."""
    return None
