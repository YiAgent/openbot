"""F-series acceptance tests for the F1 webhook-worker-layering slice.

Verifies the observable-outcome criteria from spec §8:
  F-01  decide_and_enqueue enqueues TaskSpec v3 (not QueuePayload)
  F-02  Worker routes v3 TaskSpec to execute_handler (not run_dispatch)
  F-03  Worker falls back to run_dispatch for legacy v2 QueuePayload entries
  F-04  cancel-openbot in initial_labels → worker quick-exit, no handler
  F-05  Preflight chain contains 10 middleware in the locked order
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from openbot.application.dispatcher import build_preflight_chain
from openbot.application.middleware import (
    ActorRoleMiddleware,
    AuditStartMiddleware,
    BudgetMiddleware,
    CancelCommentMiddleware,
    CancelLabelMiddleware,
    FeatureToggleMiddleware,
    ForkPRGateMiddleware,
    KillSwitchMiddleware,
    RateLimitMiddleware,
    SanitizeInputsMiddleware,
)
from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.infrastructure.queue.worker import (
    GROUP_NAME,
    STREAM_NAME,
    consume_loop,
    ensure_consumer_group,
)
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests._fakes.queue import FakeQueue
from tests.application.middleware.conftest import make_event

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_spec(initial_labels: list[str] | None = None) -> TaskSpec:
    from openbot.application.router import dispatch_for

    event = make_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None
    return TaskSpec.from_event_and_dispatch(event, dispatch, initial_labels=initial_labels or [])


# ── F-01 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f01_decide_and_enqueue_produces_task_spec_v3() -> None:
    """F-01: decide_and_enqueue enqueues TaskSpec v3, not QueuePayload."""
    from openbot.application.router import dispatch_for
    from openbot.dispatcher import decide_and_enqueue

    event = make_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None

    queue = FakeQueue()
    adapter = FakeChannelAdapter(parsed_event=event)

    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    # Should have exactly 1 TaskSpec v3 in task_specs
    assert len(queue.task_specs) == 1, f"Expected 1 task_spec, got {len(queue.task_specs)}"
    spec = queue.task_specs[0]
    assert spec.spec_version == 3
    assert spec.classifier_skipped is True

    # The old enqueue() path (QueuePayload) should NOT have been called
    assert queue.calls == [], "QueuePayload.enqueue() must NOT be called for v3 path"


# ── F-02 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f02_worker_routes_v3_to_execute_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-02: Worker calls execute_handler (not run_dispatch) for v3 TaskSpec blobs."""
    handler_calls: list[str] = []

    async def fake_execute_handler(**kw: object) -> None:
        handler_calls.append(kw["event"].delivery_id)  # type: ignore[union-attr]

    run_dispatch_calls: list[str] = []

    async def fake_run_dispatch(**kw: object) -> None:
        run_dispatch_calls.append(kw["event"].delivery_id)  # type: ignore[union-attr]

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        fake_execute_handler,
    )
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.run_dispatch",
        fake_run_dispatch,
    )
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.load_for_repo",
        FakeConfigLoader().load_for_repo,
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
        consumer_name="f02-test",
        shutdown=shutdown,
        read_block_ms=50,
    )

    assert len(handler_calls) == 1
    assert handler_calls[0] == spec.delivery_id
    assert run_dispatch_calls == [], "run_dispatch must NOT be called for v3 TaskSpec"


# ── F-03 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f03_worker_falls_back_to_run_dispatch_for_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-03: Worker routes legacy v2 QueuePayload blobs to run_dispatch."""
    from openbot.application.router import dispatch_for
    from openbot.infrastructure.queue.payload import QueuePayload

    run_dispatch_calls: list[str] = []

    async def fake_run_dispatch(**kw: object) -> None:
        run_dispatch_calls.append(kw["event"].delivery_id)  # type: ignore[union-attr]

    execute_handler_calls: list[str] = []

    async def fake_execute_handler(**kw: object) -> None:
        execute_handler_calls.append(kw["event"].delivery_id)  # type: ignore[union-attr]

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.run_dispatch",
        fake_run_dispatch,
    )
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        fake_execute_handler,
    )

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)

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
        consumer_name="f03-test",
        shutdown=shutdown,
        read_block_ms=50,
    )

    assert len(run_dispatch_calls) == 1
    assert run_dispatch_calls[0] == event.delivery_id
    assert execute_handler_calls == [], "execute_handler must NOT be called for v2 payload"


# ── F-04 ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f04_cancel_label_quick_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-04: cancel-openbot in initial_labels → worker quick-exit, no handler, PEL empty."""
    handler_calls: list[str] = []

    async def fake_execute_handler(**_kw: object) -> None:
        handler_calls.append("called")

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        fake_execute_handler,
    )
    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.load_for_repo",
        FakeConfigLoader().load_for_repo,
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
        consumer_name="f04-test",
        shutdown=shutdown,
        read_block_ms=50,
    )

    assert handler_calls == [], (
        "execute_handler must NOT be called when cancel-openbot label is set"
    )

    # PEL must be empty — entry was XACK'd
    pending = await redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 0, "Entry must be XACK'd (PEL must be empty)"


# ── F-05 ─────────────────────────────────────────────────────────────────────


def test_f05_preflight_chain_has_10_middleware_in_locked_order() -> None:
    """F-05: build_preflight_chain() returns exactly 10 middleware in the locked order."""
    chain = build_preflight_chain()

    assert len(chain) == 10, f"Expected 10 middleware, got {len(chain)}: {chain}"

    expected_types = [
        SanitizeInputsMiddleware,
        KillSwitchMiddleware,
        FeatureToggleMiddleware,
        CancelLabelMiddleware,
        CancelCommentMiddleware,
        ForkPRGateMiddleware,
        ActorRoleMiddleware,
        RateLimitMiddleware,
        BudgetMiddleware,
        AuditStartMiddleware,
    ]

    for i, (actual, expected_cls) in enumerate(zip(chain, expected_types, strict=True)):
        assert isinstance(actual, expected_cls), (
            f"Position {i}: expected {expected_cls.__name__}, got {type(actual).__name__}"
        )


# ── asyncio fixture ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _asyncio_mode_marker():
    """asyncio mode=auto; fixture is a no-op kept as a clear marker."""
    return None
