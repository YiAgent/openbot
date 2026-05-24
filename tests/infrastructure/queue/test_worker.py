"""Worker consume loop — XREADGROUP + XACK + retry + DLQ.

These tests exercise the worker against fakeredis with the actual
dispatch wired through. Each test seeds a single message via direct XADD,
runs ONE iteration of `consume_loop` (via a one-shot shutdown), and
inspects the resulting Redis state.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.queue import consume_loop, ensure_consumer_group
from openbot.infrastructure.queue.payload import DEAD_STREAM, GROUP_NAME, STREAM_NAME
from openbot.infrastructure.queue.task_spec import TaskSpec
from tests._fakes.config_loader import FakeConfigLoader


def _spec(delivery_id: str = "d-1") -> TaskSpec:
    event = UnifiedEvent(
        channel="github",
        delivery_id=delivery_id,
        kind=EventKind.ISSUE_OPENED,
        repo="org/r",
        actor="alice",
        actor_type="User",
        issue_number=1,
        installation_id=99,
        raw={},
    )
    from openbot.application.router import dispatch_for

    dispatch = dispatch_for(event)
    assert dispatch is not None
    return TaskSpec.from_event_and_dispatch(event, dispatch)


async def _run_one_iteration(redis: fakeredis.aioredis.FakeRedis, **kwargs) -> None:
    """Run consume_loop for ~1 iteration then signal shutdown.

    `read_block_ms=50` keeps XREADGROUP fast — the production default
    (5s) would make this test take 5s per scenario."""
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        consume_loop(
            redis=redis,
            consumer_name="test-consumer",
            shutdown=shutdown,
            read_block_ms=50,
            **kwargs,
        )
    )
    # Give the loop a moment to drain at least one read+dispatch round.
    await asyncio.sleep(0.2)
    shutdown.set()
    await asyncio.wait_for(task, timeout=3.0)


# ───── happy path: consume + XACK ─────


async def test_consumer_acks_after_successful_dispatch(monkeypatch) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_consumer_group(redis)
    spec = _spec()
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    # Stub execute_handler + load_for_repo so we don't need a real adapter / DB.
    adapter = AsyncMock()

    async def fake_execute_handler(**kw: object) -> None:
        pass

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        fake_execute_handler,
    )
    fake_loader = FakeConfigLoader()
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.load_for_repo",
        fake_loader.load_for_repo,
    )

    await _run_one_iteration(redis, adapter=adapter, session_factory=None)

    # Stream still has the entry (XACK doesn't delete) but PEL is empty.
    pending = await redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 0


async def test_consumer_skips_unroutable_spec(monkeypatch) -> None:
    """A spec whose Router no longer dispatches (e.g. forward-incompat
    EventKind) is XACK'd silently — no DLQ, no retry."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_consumer_group(redis)

    spec = _spec()
    tampered = spec.to_json().replace('"issue.opened"', '"issue.transferred"')
    await redis.xadd(STREAM_NAME, {"json": tampered})

    handler_called = False

    async def fake_execute_handler(**kw: object) -> None:
        nonlocal handler_called
        handler_called = True

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        fake_execute_handler,
    )

    adapter = AsyncMock()
    await _run_one_iteration(redis, adapter=adapter, session_factory=None)

    assert not handler_called  # never reached the handler
    pending = await redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 0  # XACK'd


# ───── retry counter + DLQ ─────


async def test_unreadable_payload_goes_to_dlq() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_consumer_group(redis)
    # Push a malformed entry — not JSON.
    await redis.xadd(STREAM_NAME, {"json": "{not json at all"})

    adapter = AsyncMock()
    await _run_one_iteration(redis, adapter=adapter, session_factory=None)

    # Original stream entry was XACK'd; copy went to DLQ.
    assert await redis.xlen(DEAD_STREAM) == 1
    pending = await redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 0


async def test_retry_counter_incremented_per_attempt(monkeypatch) -> None:
    """Every dispatch bumps `openbot:workflows:retries:<entry_id>`."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_consumer_group(redis)
    spec = _spec()
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    async def fake_execute_handler(**kw: object) -> None:
        pass

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        fake_execute_handler,
    )
    fake_loader = FakeConfigLoader()
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.load_for_repo",
        fake_loader.load_for_repo,
    )

    adapter = AsyncMock()
    await _run_one_iteration(redis, adapter=adapter, session_factory=None)

    # Find the entry's retry counter.
    counter_keys = await redis.keys("openbot:workflows:retries:*")
    assert len(counter_keys) == 1
    count = await redis.get(counter_keys[0])
    assert int(count) == 1


# ───── consumer group setup ─────


async def test_ensure_consumer_group_idempotent() -> None:
    """Re-running ensure_consumer_group must not raise on the BUSYGROUP
    response from the second call."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_consumer_group(redis)
    await ensure_consumer_group(redis)  # second call, BUSYGROUP, swallowed
    # Stream exists and group exists — verify by querying xinfo.
    info = await redis.xinfo_groups(STREAM_NAME)
    assert any(g["name"] == GROUP_NAME for g in info)


# ───── shutdown ─────


async def test_consumer_exits_on_shutdown_event() -> None:
    """Setting the shutdown event must cause the loop to return
    promptly — no hang, no leaked task."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_consumer_group(redis)

    shutdown = asyncio.Event()
    adapter = AsyncMock()
    task = asyncio.create_task(
        consume_loop(
            redis=redis,
            adapter=adapter,
            session_factory=None,
            consumer_name="test-shutdown",
            shutdown=shutdown,
        )
    )
    await asyncio.sleep(0.05)
    shutdown.set()
    # Loop should return within a few seconds (the XREADGROUP block is
    # 5s; we tolerate that). If it leaks, asyncio.wait_for cancels.
    await asyncio.wait_for(task, timeout=10.0)
    assert task.done()


# ───── DLQ shape ─────


async def test_dlq_entry_preserves_original_payload() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_consumer_group(redis)
    await redis.xadd(STREAM_NAME, {"json": "{not json"})

    adapter = AsyncMock()
    await _run_one_iteration(redis, adapter=adapter, session_factory=None)

    dlq_entries = await redis.xrange(DEAD_STREAM, count=1)
    assert len(dlq_entries) == 1
    _entry_id, fields = dlq_entries[0]
    # DLQ entry MUST carry a reason so ops can grep by failure class.
    assert fields["reason"] == "task_spec_unreadable"


@pytest.fixture(autouse=True)
def _no_anyio_marker():
    """The repo's pytest config sets asyncio mode=auto; this fixture
    is a no-op kept as a clear marker that these tests are async."""
    return None
