"""Worker: v3 TaskSpec routing, W1 cancel quick-check."""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.infrastructure.queue.worker import (
    GROUP_NAME,
    STREAM_NAME,
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
    """cancel-openbot in initial_labels -> XACK immediately, no handler call."""
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
async def test_worker_stores_last_reviewed_sha_after_successful_review(
    monkeypatch,
) -> None:
    """After a REVIEW spec completes, worker writes the head SHA to task_runs."""
    from openbot.application.state.runs_repo import get_last_reviewed_sha, transition
    from openbot.domain.events import EventKind, UnifiedEvent
    from openbot.infrastructure.persistence import (
        create_schema,
        make_engine,
        make_session_factory,
    )
    from tests._fakes.config_loader import FakeConfigLoader

    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    sf = make_session_factory(engine)

    # Seed a task_runs row for the PR resource so store_reviewed_sha has a row.
    pr_event = UnifiedEvent(
        channel="github",
        delivery_id="seed-del",
        kind=EventKind.PR_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        pr_number=7,
        event_seq=1_000,
    )
    async with sf() as session:
        await transition(session, event=pr_event, new_run_id="run-seed")
        await session.commit()

    # Build a review TaskSpec with head SHA in raw.
    head_sha = "SHA-to-store-abc123"
    from openbot.application.router import dispatch_for
    from openbot.infrastructure.queue.task_spec import TaskSpec

    raw = {
        "pull_request": {
            "number": 7,
            "additions": 10,
            "deletions": 2,
            "changed_files": 1,
            "head": {"sha": head_sha},
            "base": {"sha": "SHA-base"},
        }
    }
    review_event = UnifiedEvent(
        channel="github",
        delivery_id="del-review-worker-test",
        kind=EventKind.PR_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        pr_number=7,
        installation_id=100,
        raw=raw,
    )
    dispatch = dispatch_for(review_event)
    assert dispatch is not None
    spec = TaskSpec.from_event_and_dispatch(review_event, dispatch, initial_labels=[])
    # resource_key must be set so the worker can write back.
    spec = dataclasses.replace(spec, resource_key=review_event.resource_key)

    async def fake_execute_handler(**_kw: object) -> None:
        pass  # Simulate successful review handler

    monkeypatch.setattr("openbot.infrastructure.queue.worker.execute_handler", fake_execute_handler)
    fake_loader = FakeConfigLoader()
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.load_for_repo", fake_loader.load_for_repo
    )

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_running_loop().call_later(0.3, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=AsyncMock(),
        session_factory=sf,
        consumer_name="test-reviewed-sha",
        shutdown=shutdown,
        read_block_ms=50,
    )

    # Verify the SHA was persisted.
    async with sf() as session:
        stored = await get_last_reviewed_sha(session, review_event.resource_key or "")
    assert stored == head_sha

    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_checkpointer_threaded_to_execute_handler(monkeypatch) -> None:
    """agent_checkpointer sentinel flows consume_loop -> execute_handler."""
    from tests._fakes.config_loader import FakeConfigLoader

    sentinel = object()
    captured: list[object] = []

    async def fake_execute_handler(**kw: object) -> None:
        captured.append(kw.get("agent_checkpointer"))

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
        consumer_name="test-checkpointer-thread",
        shutdown=shutdown,
        read_block_ms=50,
        agent_checkpointer=sentinel,
    )

    assert len(captured) == 1
    assert captured[0] is sentinel


@pytest.fixture(autouse=True)
def _no_anyio_marker():
    """asyncio mode=auto; fixture is a no-op kept as a clear marker."""
    return None
