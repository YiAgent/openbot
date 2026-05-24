"""Worker: CancelledError handling for SUPERSEDE / shutdown paths.

When a handler is cancelled (via ``cancellation.signal`` or process
shutdown), ``execute_handler`` re-raises ``CancelledError`` after
finalizing the GitHub check run. The worker must:

  - Treat the entry as DONE (XACK it) so reclaim doesn't re-run a
    cancelled task in 60s.
  - Still call ``cancellation_deregister`` (already in ``finally``).
  - Not write to the DLQ — cancellation is a normal lifecycle event,
    not a failure.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.infrastructure.queue.worker import (
    DEAD_STREAM,
    GROUP_NAME,
    STREAM_NAME,
    consume_loop,
    ensure_consumer_group,
)
from tests.application.middleware.conftest import make_event


def _make_spec() -> TaskSpec:
    from openbot.application.router import dispatch_for

    event = make_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None
    return TaskSpec.from_event_and_dispatch(event, dispatch, check_run_id=99)


@pytest.mark.asyncio
async def test_worker_xacks_on_handler_cancelled(monkeypatch) -> None:
    """A cancelled handler must XACK the entry so reclaim doesn't re-run it."""
    from tests._fakes.config_loader import FakeConfigLoader

    async def cancelling_execute_handler(**_kw: object) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        cancelling_execute_handler,
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
    asyncio.get_running_loop().call_later(0.20, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=AsyncMock(),
        session_factory=None,
        consumer_name="test-cancel",
        shutdown=shutdown,
        read_block_ms=50,
    )

    # No PEL entries left → entry was XACK'd, not abandoned for reclaim.
    pending = await redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 0

    # Cancellation is not a failure — DLQ must stay empty.
    dead_len = await redis.xlen(DEAD_STREAM)
    assert dead_len == 0


@pytest.mark.asyncio
async def test_worker_dlqs_after_max_attempts_on_exception(monkeypatch) -> None:
    """Plain Exception path still hits DLQ after _MAX_ATTEMPTS — regression guard.

    Ensures the new BaseException split in the worker did not accidentally
    swallow the existing retry/DLQ behaviour for non-cancellation crashes.
    """
    from openbot.infrastructure.queue import worker as worker_mod
    from tests._fakes.config_loader import FakeConfigLoader

    async def crashing_execute_handler(**_kw: object) -> None:
        raise RuntimeError("handler bug")

    monkeypatch.setattr(worker_mod, "execute_handler", crashing_execute_handler)
    fake_loader = FakeConfigLoader()
    monkeypatch.setattr(worker_mod, "load_for_repo", fake_loader.load_for_repo)
    # Force the very first attempt to count as "max attempts reached" so the
    # test doesn't have to round-trip the entry three times.
    monkeypatch.setattr(worker_mod, "_MAX_ATTEMPTS", 1)

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)
    spec = _make_spec()
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_running_loop().call_later(0.20, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=AsyncMock(),
        session_factory=None,
        consumer_name="test-dlq",
        shutdown=shutdown,
        read_block_ms=50,
    )

    dead_len = await redis.xlen(DEAD_STREAM)
    assert dead_len == 1
