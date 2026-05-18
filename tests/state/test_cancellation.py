"""``state.cancellation`` — registry, Redis flag, checkpoint semantics.

Three orthogonal behaviours under test:

* In-process: ``register`` + ``signal`` cancels a locally-running task.
* Cross-dyno: ``signal`` sets the Redis flag; ``checkpoint`` raises
  ``RunCancelledError`` (a subclass of ``CancelledError``) when the flag
  is present, including in a process that never called ``register``.
* Hygiene: ``deregister`` clears the slot; ``redis=None`` still cancels
  the local task; flag read failures fall closed (no spurious raise).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import fakeredis.aioredis
import pytest

from openbot.state.cancellation import (
    RunCancelledError,
    checkpoint,
    clear_for_tests,
    deregister,
    is_cancelled,
    register,
    signal,
)


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture(autouse=True)
def _wipe_registry_between_tests() -> None:
    clear_for_tests()


# ───── in-process registry ─────


async def test_signal_cancels_local_task(redis: fakeredis.aioredis.FakeRedis) -> None:
    """A running asyncio.Task registered under run_id is cancelled by signal()."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def worker() -> None:
        register("run-1")
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            deregister("run-1")

    task = asyncio.create_task(worker())
    await started.wait()
    await signal(redis, "run-1")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


async def test_deregister_prevents_spurious_cancel(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Once deregister has run, signal() must not touch a fresh task using the same id."""
    fresh_done = asyncio.Event()

    async def short_lived() -> None:
        register("run-1")
        deregister("run-1")

    await short_lived()

    async def fresh_task() -> None:
        # New unrelated task — must not be cancelled by the stale signal.
        await asyncio.sleep(0)
        fresh_done.set()

    task = asyncio.create_task(fresh_task())
    await signal(redis, "run-1")  # no-op locally; flag still sets
    await asyncio.wait_for(task, timeout=1.0)
    assert fresh_done.is_set()


# ───── cross-dyno Redis flag ─────


async def test_signal_sets_flag_and_checkpoint_raises(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await signal(redis, "run-1")
    assert await is_cancelled(redis, "run-1") is True
    with pytest.raises(RunCancelledError):
        await checkpoint(redis, "run-1")


async def test_checkpoint_no_flag_is_a_noop(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await checkpoint(redis, "run-never-signalled")  # must not raise


async def test_run_cancelled_error_is_cancelled_error_subclass() -> None:
    """Existing ``except CancelledError`` blocks still catch RunCancelledError."""
    err = RunCancelledError("my-reason")
    assert isinstance(err, asyncio.CancelledError)
    assert err.reason == "my-reason"


async def test_is_cancelled_with_none_redis_returns_false() -> None:
    """redis=None means cross-dyno signalling is unavailable — fall closed."""
    assert await is_cancelled(None, "run-1") is False


async def test_signal_with_none_redis_still_cancels_local_task() -> None:
    """Even without Redis, the in-process registry path still works."""
    started = asyncio.Event()

    async def worker() -> None:
        register("run-1")
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            deregister("run-1")

    task = asyncio.create_task(worker())
    await started.wait()
    await signal(None, "run-1")
    with pytest.raises(asyncio.CancelledError):
        await task
