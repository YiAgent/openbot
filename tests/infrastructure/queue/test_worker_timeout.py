"""Worker timeout + DLQ check-run close — unit tests.

Covers the two new paths in ``_execute_task_spec``:

1. ``asyncio.TimeoutError``: execute_handler timed out.
   - Entry must be XACKed (not retried).
   - check_run close is already handled by execute_handler's CancelledError
     path; the worker just XACKs cleanly.

2. ``Exception`` escaped + ``attempts >= _MAX_ATTEMPTS``:
   - Entry must be XACKed + DLQ'd.
   - When ``spec.check_run_id`` is set, adapter.update_check_run must be
     called with ``conclusion="failure"`` BEFORE ``_ack_and_dlq``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from openbot.application.router import dispatch_for
from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.infrastructure.queue.worker import (
    DEAD_STREAM,
    GROUP_NAME,
    STREAM_NAME,
    consume_loop,
    ensure_consumer_group,
)
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests.application.middleware.conftest import make_event


def _make_spec(*, check_run_id: int | None = None) -> TaskSpec:
    event = make_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None
    return TaskSpec.from_event_and_dispatch(event, dispatch, check_run_id=check_run_id)


async def _run_once(redis, adapter, monkeypatch, *, execute_side_effect):
    """Boot the consumer loop for one tick with a patched execute_handler.

    Callers must call ``ensure_consumer_group`` and ``redis.xadd`` in that
    order *before* invoking this helper — the consumer group must exist before
    the entry is added so that ``XREADGROUP >`` sees it (group's ``last-id``
    would otherwise point at the entry's own ID, skipping it).
    """
    from tests._fakes.config_loader import FakeConfigLoader

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        AsyncMock(side_effect=execute_side_effect),
    )
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.load_for_repo",
        FakeConfigLoader().load_for_repo,
    )

    shutdown = asyncio.Event()
    asyncio.get_running_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=adapter,
        session_factory=None,
        consumer_name="test-timeout",
        shutdown=shutdown,
        read_block_ms=50,
    )


# ─── timeout path ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_entry_xacked_not_retried(monkeypatch) -> None:
    """When execute_handler raises TimeoutError the entry is XACKed (no retry)."""
    redis = fakeredis.aioredis.FakeRedis()
    adapter = FakeChannelAdapter()
    spec = _make_spec(check_run_id=None)
    # Group must be created BEFORE xadd so the entry is visible to XREADGROUP ">".
    await ensure_consumer_group(redis)
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    await _run_once(redis, adapter, monkeypatch, execute_side_effect=TimeoutError())

    # PEL must be empty — entry was XACKed
    pending = await redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_timeout_does_not_go_to_dlq(monkeypatch) -> None:
    """TimeoutError must not add an entry to the dead-letter queue."""
    redis = fakeredis.aioredis.FakeRedis()
    adapter = FakeChannelAdapter()
    spec = _make_spec(check_run_id=None)
    # Group before xadd so the entry is delivered (not skipped as "already-seen").
    await ensure_consumer_group(redis)
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    await _run_once(redis, adapter, monkeypatch, execute_side_effect=TimeoutError())

    dlq_len = await redis.xlen(DEAD_STREAM)
    assert dlq_len == 0


# ─── DLQ check-run close path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dlq_closes_check_run_on_max_attempts(monkeypatch) -> None:
    """When max attempts are exceeded and check_run_id is set,
    update_check_run is called with conclusion='failure' before DLQ."""
    redis = fakeredis.aioredis.FakeRedis()
    adapter = FakeChannelAdapter()
    spec = _make_spec(check_run_id=99)

    from openbot.infrastructure.queue import worker as _worker
    from tests._fakes.config_loader import FakeConfigLoader

    # Create the consumer group BEFORE adding entries so XREADGROUP ">" delivers
    # them — the group is created with id="$" which resolves to 0-0 on an empty
    # stream, meaning all subsequently-added entries have higher IDs and are visible.
    await ensure_consumer_group(redis)
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    # Force attempt counter to _MAX_ATTEMPTS - 1 so the next run tips it over.
    retry_key = f"openbot:workflows:retries:{(await redis.xrange(STREAM_NAME))[0][0].decode()}"
    await redis.set(retry_key, _worker._MAX_ATTEMPTS - 1)

    # Simulate a handler crash to trigger the DLQ path.
    call_count = 0

    async def always_crash(**kw):
        nonlocal call_count
        call_count += 1
        raise RuntimeError(f"crash #{call_count}")

    monkeypatch.setattr("openbot.infrastructure.queue.worker.execute_handler", always_crash)
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.load_for_repo",
        FakeConfigLoader().load_for_repo,
    )

    shutdown = asyncio.Event()
    asyncio.get_running_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=adapter,
        session_factory=None,
        consumer_name="test-dlq-cr",
        shutdown=shutdown,
        read_block_ms=50,
    )

    # check_run must have been closed as failure
    assert len(adapter.check_run_updates) == 1
    cr = adapter.check_run_updates[0]
    assert cr["check_run_id"] == 99
    assert cr["status"] == "completed"
    assert cr["conclusion"] == "failure"

    # Entry moved to DLQ
    dlq_len = await redis.xlen(DEAD_STREAM)
    assert dlq_len == 1


@pytest.mark.asyncio
async def test_dlq_no_check_run_update_when_id_absent(monkeypatch) -> None:
    """DLQ path must not call update_check_run when check_run_id is None."""
    redis = fakeredis.aioredis.FakeRedis()
    adapter = FakeChannelAdapter()
    spec = _make_spec(check_run_id=None)

    from openbot.infrastructure.queue import worker as _worker
    from tests._fakes.config_loader import FakeConfigLoader

    # Consumer group must be created BEFORE xadd so the entry is visible
    # to XREADGROUP ">" (group's last-id = 0-0 on the empty stream).
    await ensure_consumer_group(redis)
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    entry_id = (await redis.xrange(STREAM_NAME))[0][0].decode()
    retry_key = f"openbot:workflows:retries:{entry_id}"
    await redis.set(retry_key, _worker._MAX_ATTEMPTS - 1)

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        AsyncMock(side_effect=RuntimeError("crash")),
    )
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.load_for_repo",
        FakeConfigLoader().load_for_repo,
    )

    shutdown = asyncio.Event()
    asyncio.get_running_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=adapter,
        session_factory=None,
        consumer_name="test-dlq-no-cr",
        shutdown=shutdown,
        read_block_ms=50,
    )

    assert adapter.check_run_updates == []
    assert await redis.xlen(DEAD_STREAM) == 1
