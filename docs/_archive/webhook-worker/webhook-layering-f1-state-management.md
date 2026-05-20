# Webhook-Worker Layering — F1: Chain Front-Migration (Part 2b/3: Worker v3 path + Tests)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Part 1:** `2026-05-20-webhook-worker-layering-f1-part1.md` — Tasks 1–3 (schema, QueuePort, execute_handler)
>
> **Part 2a:** `2026-05-20-webhook-worker-layering-f1-part2a.md` — Tasks 4–5 (decide_and_enqueue, BackgroundTask)

**Goal:** Add v3 routing to the worker, write F-series acceptance tests, and verify the full baseline is still passing.

**Spec:** `docs/specs/2026-05-17-webhook-worker-layering-design.md` §3 (worker flow W1-W8) and §8 F1 acceptance criteria.

---

## Task 6: Worker v3 path

**Files:**
- Modify: `openbot/infrastructure/queue/worker.py`
- Create: `tests/infrastructure/queue/test_worker_v3.py`

The worker currently always deserializes a `QueuePayload`. Add a fast peek (`_is_v3_spec`) before the existing deserialization attempt, and route v3 blobs through the new `_execute_task_spec` helper.

- [ ] **Step 1: Write failing tests**

```python
# tests/infrastructure/queue/test_worker_v3.py
"""Worker: v3 TaskSpec routing, W1 cancel quick-check, legacy fallback."""
from __future__ import annotations
import asyncio
import json
import pytest
import fakeredis.aioredis
from openbot.infrastructure.queue.worker import (
    GROUP_NAME,
    STREAM_NAME,
    _is_v3_spec,
    consume_loop,
    ensure_consumer_group,
)
from openbot.infrastructure.queue.task_spec import TaskSpec
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests.conftest import make_issue_opened_event


def _make_spec(initial_labels: list[str] | None = None) -> TaskSpec:
    from openbot.application.router import dispatch_for
    event = make_issue_opened_event()
    dispatch = dispatch_for(event)
    assert dispatch is not None
    return TaskSpec.from_event_and_dispatch(
        event, dispatch, initial_labels=initial_labels or []
    )


def test_is_v3_spec_true() -> None:
    assert _is_v3_spec(json.dumps({"spec_version": 3})) is True


def test_is_v3_spec_false_for_v2_payload() -> None:
    assert _is_v3_spec(json.dumps({"version": 2, "task_id": "x"})) is False


def test_is_v3_spec_false_for_garbage() -> None:
    assert _is_v3_spec("not-json") is False
    assert _is_v3_spec(None) is False


@pytest.mark.asyncio
async def test_worker_routes_v3_to_execute_handler(monkeypatch) -> None:
    """Worker calls execute_handler (not run_dispatch) for v3 specs."""
    handler_calls: list[str] = []

    async def fake_execute_handler(**kw) -> None:
        handler_calls.append(kw["event"].delivery_id)

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        fake_execute_handler,
    )

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)
    spec = _make_spec()
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_event_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=FakeChannelAdapter(),
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
    handler_calls: list[str] = []

    async def fake_execute_handler(**kw) -> None:
        handler_calls.append("called")

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.execute_handler",
        fake_execute_handler,
    )

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)
    spec = _make_spec(initial_labels=["cancel-openbot"])
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_event_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=FakeChannelAdapter(),
        session_factory=None,
        consumer_name="test-cancel",
        shutdown=shutdown,
        read_block_ms=50,
    )

    assert handler_calls == []  # no handler called
    # Entry must be XACK'd — nothing left in the PEL
    pending = await redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_worker_legacy_v2_payload_still_works(monkeypatch) -> None:
    """v2 QueuePayload entries continue through the old path unchanged."""
    run_dispatch_calls: list[str] = []

    async def fake_run_dispatch(**kw) -> None:
        run_dispatch_calls.append(kw["event"].delivery_id)

    monkeypatch.setattr(
        "openbot.infrastructure.queue.worker.run_dispatch",
        fake_run_dispatch,
    )

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)

    # Build a v2 payload using the existing infrastructure
    from openbot.infrastructure.queue.payload import QueuePayload
    from openbot.domain.workflows import Feature
    event = make_issue_opened_event()
    from openbot.application.router import dispatch_for
    dispatch = dispatch_for(event)
    assert dispatch is not None
    payload = QueuePayload.from_event(
        event,
        feature=dispatch.feature,
        task_id=dispatch.task_id,
    )
    await redis.xadd(STREAM_NAME, {"json": payload.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_event_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis,
        adapter=FakeChannelAdapter(),
        session_factory=None,
        consumer_name="test-legacy",
        shutdown=shutdown,
        read_block_ms=50,
    )

    assert len(run_dispatch_calls) == 1
```

- [ ] **Step 2: Run — expect ImportError on `_is_v3_spec` and `execute_handler`**

```bash
cd /Users/wy/projects/openbot && uv run pytest tests/infrastructure/queue/test_worker_v3.py -v 2>&1 | head -15
```

- [ ] **Step 3: Add v3 imports and helpers to worker.py**

Add to the import block at the top of `openbot/infrastructure/queue/worker.py` (after the existing imports):

```python
import json  # add if not already present

from openbot.application.dispatcher import execute_handler
from openbot.infrastructure.config_loader import load_for_repo
from openbot.infrastructure.queue.task_spec import TaskSpec, deserialize_task_spec
```

Also add `upgrade_dispatch` if not already imported (it's used below):
```python
# already imported: from openbot.application.router import dispatch_for, upgrade_dispatch
```

- [ ] **Step 4: Add _is_v3_spec() helper to worker.py**

Add this function after `_retry_key()` (around line 91):

```python
def _is_v3_spec(blob: str | bytes | None) -> bool:
    """Peek at the stream entry's JSON to see if it is a TaskSpec v3.

    Returns True only when ``spec_version == 3`` is found, without
    attempting a full deserialisation. Keeps the v2 path fast for the
    existing majority of entries during the rolling migration.
    """
    if blob is None:
        return False
    try:
        text = blob.decode("utf-8") if isinstance(blob, bytes | bytearray) else blob
        data = json.loads(text)
        return isinstance(data, dict) and data.get("spec_version") == 3
    except Exception:
        return False
```

- [ ] **Step 5: Add _execute_task_spec() to worker.py**

Add this function after `_attach_sentry_tags()`:

```python
async def _execute_task_spec(
    spec: TaskSpec,
    *,
    entry_id: str,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    """W1-W8: Process one TaskSpec v3 entry.

    W1: cancel quick-check via initial_labels.
    W2: reconstruct UnifiedEvent from spec.
    W3: reconstruct Dispatch from event via router.
    W4: load EffectiveConfig for this repo.
    W5: call execute_handler() (no preflight — done at webhook time).
    W6-W8: bump attempt counter, register/deregister cancellation slot.
    """
    # W1: Cancel quick-check — avoid calling the handler at all.
    if "cancel-openbot" in spec.initial_labels:
        _logger.info(
            "queue_v3_cancel_quick_exit",
            extra={"entry_id": entry_id, "delivery_id": spec.delivery_id},
        )
        await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
        return

    # W2: Reconstruct event from the spec's serialised fields.
    event = spec.to_event()

    # W3: Re-derive Dispatch from the router (pure; no I/O).
    new_dispatch = dispatch_for(event)
    if new_dispatch is None:
        _logger.info(
            "queue_v3_entry_no_longer_routable",
            extra={"entry_id": entry_id, "delivery_id": spec.delivery_id},
        )
        await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
        return

    # Carry state-machine fields from the spec into Dispatch.
    if spec.resource_key is not None and spec.intent not in (None, "start"):
        new_dispatch = upgrade_dispatch(
            new_dispatch,
            intent=spec.intent,
            run_id=spec.run_id,
            prev_run_id=spec.prev_run_id,
            event_seq=spec.event_seq,
            resource_key=spec.resource_key,
        )

    # W4: Load effective config.
    config = await load_for_repo(None, event)

    # W5-W8: Attempt counter + cancellation lifecycle.
    attempts = await _bump_attempt_counter(redis, entry_id)
    active_run_id = new_dispatch.run_id or new_dispatch.task_id
    cancellation_register(active_run_id)

    try:
        try:
            await execute_handler(
                adapter=adapter,
                event=event,
                dispatch=new_dispatch,
                config=config,
                session_factory=session_factory,
                redis=redis,
                check_run_id=spec.check_run_id,
            )
        except Exception:
            _logger.exception(
                "queue_v3_execute_handler_escaped",
                extra={"entry_id": entry_id, "delivery_id": spec.delivery_id},
            )
            if attempts >= _MAX_ATTEMPTS:
                await _ack_and_dlq(redis, entry_id, reason="max_attempts_v3")
            return
    finally:
        cancellation_deregister(active_run_id)

    await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
    _logger.info(
        "queue_v3_entry_dispatched",
        extra={
            "entry_id": entry_id,
            "delivery_id": spec.delivery_id,
            "repo": spec.repo,
            "scenario": spec.scenario,
            "attempts": attempts,
        },
    )
```

- [ ] **Step 6: Modify _process_entry() to branch on v3**

In `_process_entry()`, replace:

```python
    blob = _extract_payload_blob(fields)
    payload = deserialize_payload(blob) if blob is not None else None
    if payload is None:
        await _ack_and_dlq(redis, entry_id, reason="payload_unreadable")
        return
```

With:

```python
    blob = _extract_payload_blob(fields)
    if blob is None:
        await _ack_and_dlq(redis, entry_id, reason="payload_unreadable")
        return

    # Route TaskSpec v3 entries through the new path; fall through to the
    # legacy QueuePayload path for v1/v2 entries still in the queue.
    if _is_v3_spec(blob):
        spec = deserialize_task_spec(blob)
        if spec is None:
            await _ack_and_dlq(redis, entry_id, reason="task_spec_v3_unreadable")
            return
        await _execute_task_spec(
            spec,
            entry_id=entry_id,
            redis=redis,
            adapter=adapter,
            session_factory=session_factory,
        )
        return

    payload = deserialize_payload(blob)
    if payload is None:
        await _ack_and_dlq(redis, entry_id, reason="payload_unreadable")
        return
```

- [ ] **Step 7: Run v3 worker tests — expect all PASS**

```bash
cd /Users/wy/projects/openbot && uv run pytest tests/infrastructure/queue/test_worker_v3.py -v
```

- [ ] **Step 8: Run full suite**

```bash
cd /Users/wy/projects/openbot && make check 2>&1 | tail -5
```

Expected: ≥705 tests, all PASS.

- [ ] **Step 9: Commit**

```bash
cd /Users/wy/projects/openbot && git add openbot/infrastructure/queue/worker.py tests/infrastructure/queue/test_worker_v3.py
git commit -m "feat(worker): add v3 TaskSpec routing path (W1-W8)"
```

---

## Task 7: F-series acceptance tests

**Files:**
- Create: `tests/application/dispatcher/test_f_series.py`

These tests verify the F1 acceptance criteria listed in spec §8. They test observable outcomes (what gets enqueued, which path the worker takes) rather than implementation details.

- [ ] **Step 1: Write F-series tests**

```python
# tests/application/dispatcher/test_f_series.py
"""F-series acceptance tests — spec §8 F1 acceptance criteria.

F-01: Webhook async segment produces a TaskSpec v3 on the queue (not a QueuePayload).
F-02: Worker routes a v3 blob to execute_handler, not run_dispatch.
F-03: Worker processes a v2 QueuePayload entry through the legacy run_dispatch path.
F-04: cancel-openbot in initial_labels causes quick-exit (no handler call, entry XACK'd).
F-05: Preflight chain contains 10 middleware in the locked order.
"""
from __future__ import annotations
import asyncio
import pytest
import fakeredis.aioredis
from openbot.dispatcher import decide_and_enqueue
from openbot.infrastructure.queue.task_spec import TASK_SPEC_VERSION
from openbot.infrastructure.queue.worker import (
    GROUP_NAME,
    STREAM_NAME,
    consume_loop,
    ensure_consumer_group,
)
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests._fakes.queue import FakeQueue
from tests.conftest import make_issue_opened_event


# ---------------------------------------------------------------------------
# F-01: Webhook async segment enqueues TaskSpec v3 (not QueuePayload v2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_f01_webhook_async_enqueues_task_spec_v3() -> None:
    """F-01: decide_and_enqueue puts a TaskSpec v3 (not a QueuePayload) on the queue."""
    event = make_issue_opened_event()
    from openbot.application.router import dispatch_for
    dispatch = dispatch_for(event)
    assert dispatch is not None
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    assert len(queue.task_specs) == 1, "Expected exactly one TaskSpec on the queue"
    assert len(queue.calls) == 0, "QueuePayload enqueue() must NOT be called"
    assert queue.task_specs[0].spec_version == TASK_SPEC_VERSION
    assert queue.task_specs[0].classifier_skipped is True  # F1 always True


# ---------------------------------------------------------------------------
# F-02: Worker routes v3 blob to execute_handler (not run_dispatch)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_f02_worker_routes_v3_to_execute_handler(monkeypatch) -> None:
    """F-02: Worker calls execute_handler for a v3 TaskSpec blob."""
    execute_calls: list[str] = []
    run_dispatch_calls: list[str] = []

    async def fake_execute_handler(**kw) -> None:
        execute_calls.append(kw["event"].delivery_id)

    async def fake_run_dispatch(**kw) -> None:
        run_dispatch_calls.append(kw["event"].delivery_id)

    monkeypatch.setattr("openbot.infrastructure.queue.worker.execute_handler", fake_execute_handler)
    monkeypatch.setattr("openbot.infrastructure.queue.worker.run_dispatch", fake_run_dispatch)

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)

    from openbot.application.router import dispatch_for
    from openbot.infrastructure.queue.task_spec import TaskSpec
    event = make_issue_opened_event()
    spec = TaskSpec.from_event_and_dispatch(event, dispatch_for(event))
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_event_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis, adapter=FakeChannelAdapter(), session_factory=None,
        consumer_name="f02", shutdown=shutdown, read_block_ms=50,
    )

    assert execute_calls == [spec.delivery_id]
    assert run_dispatch_calls == []


# ---------------------------------------------------------------------------
# F-03: Worker processes v2 QueuePayload through legacy run_dispatch path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_f03_worker_legacy_v2_path(monkeypatch) -> None:
    """F-03: A v2 QueuePayload still goes through run_dispatch (legacy path)."""
    run_dispatch_calls: list[str] = []
    execute_calls: list[str] = []

    async def fake_run_dispatch(**kw) -> None:
        run_dispatch_calls.append(kw["event"].delivery_id)

    async def fake_execute_handler(**kw) -> None:
        execute_calls.append(kw["event"].delivery_id)

    monkeypatch.setattr("openbot.infrastructure.queue.worker.run_dispatch", fake_run_dispatch)
    monkeypatch.setattr("openbot.infrastructure.queue.worker.execute_handler", fake_execute_handler)

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)

    from openbot.infrastructure.queue.payload import QueuePayload
    event = make_issue_opened_event()
    from openbot.application.router import dispatch_for
    dispatch = dispatch_for(event)
    assert dispatch is not None
    payload = QueuePayload.from_event(event, feature=dispatch.feature, task_id=dispatch.task_id)
    await redis.xadd(STREAM_NAME, {"json": payload.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_event_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis, adapter=FakeChannelAdapter(), session_factory=None,
        consumer_name="f03", shutdown=shutdown, read_block_ms=50,
    )

    assert len(run_dispatch_calls) == 1
    assert execute_calls == []


# ---------------------------------------------------------------------------
# F-04: cancel-openbot label in initial_labels causes quick-exit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_f04_cancel_openbot_quick_exit(monkeypatch) -> None:
    """F-04: cancel-openbot in initial_labels → XACK, handler never called."""
    handler_calls: list[str] = []

    async def fake_execute_handler(**kw) -> None:
        handler_calls.append("called")

    monkeypatch.setattr("openbot.infrastructure.queue.worker.execute_handler", fake_execute_handler)

    redis = fakeredis.aioredis.FakeRedis()
    await ensure_consumer_group(redis)

    from openbot.application.router import dispatch_for
    from openbot.infrastructure.queue.task_spec import TaskSpec
    event = make_issue_opened_event()
    spec = TaskSpec.from_event_and_dispatch(
        event, dispatch_for(event), initial_labels=["cancel-openbot"]
    )
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    shutdown = asyncio.Event()
    asyncio.get_event_loop().call_later(0.15, shutdown.set)
    await consume_loop(
        redis=redis, adapter=FakeChannelAdapter(), session_factory=None,
        consumer_name="f04", shutdown=shutdown, read_block_ms=50,
    )

    assert handler_calls == []
    pending = await redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 0


# ---------------------------------------------------------------------------
# F-05: Preflight chain order is locked — 10 middleware, same order
# ---------------------------------------------------------------------------

def test_f05_preflight_chain_order_unchanged() -> None:
    """F-05: build_preflight_chain() returns the 10 locked middleware in order."""
    from openbot.application.dispatcher import build_preflight_chain
    from openbot.application.middleware import (
        SanitizeInputsMiddleware, KillSwitchMiddleware, FeatureToggleMiddleware,
        CancelLabelMiddleware, CancelCommentMiddleware, ForkPRGateMiddleware,
        ActorRoleMiddleware, RateLimitMiddleware, BudgetMiddleware, AuditStartMiddleware,
    )
    chain = build_preflight_chain()
    assert len(chain) == 10
    expected_types = [
        SanitizeInputsMiddleware, KillSwitchMiddleware, FeatureToggleMiddleware,
        CancelLabelMiddleware, CancelCommentMiddleware, ForkPRGateMiddleware,
        ActorRoleMiddleware, RateLimitMiddleware, BudgetMiddleware, AuditStartMiddleware,
    ]
    for i, (actual, expected) in enumerate(zip(chain, expected_types)):
        assert isinstance(actual, expected), (
            f"Chain position {i}: expected {expected.__name__}, got {type(actual).__name__}"
        )
```

- [ ] **Step 2: Run F-series tests — expect 5 PASS**

```bash
cd /Users/wy/projects/openbot && uv run pytest tests/application/dispatcher/test_f_series.py -v
```

- [ ] **Step 3: Run full suite**

```bash
cd /Users/wy/projects/openbot && make check 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
cd /Users/wy/projects/openbot && git add tests/application/dispatcher/test_f_series.py
git commit -m "test(f-series): add F-01..F-05 acceptance tests for F1 slice"
```

---

## Task 8: Final verification and spec amendment note

**Files:**
- No code changes — verification only.

- [ ] **Step 1: Full suite with verbose output**

```bash
cd /Users/wy/projects/openbot && make check 2>&1 | tail -10
```

Expected output (numbers will be higher due to new tests):
```
========================= N passed in X.Xs =========================
```

N ≥ 720 (705 baseline + ~15 new tests across Tasks 1–7).

- [ ] **Step 2: Verify no run_dispatch calls in webhook route**

```bash
cd /Users/wy/projects/openbot && grep -n "run_dispatch" openbot/entrypoints/api/routes/github_webhook.py
```

Expected: no matches (the import and call are gone).

- [ ] **Step 3: Verify worker imports execute_handler**

```bash
cd /Users/wy/projects/openbot && grep -n "execute_handler\|_is_v3_spec\|_execute_task_spec" openbot/infrastructure/queue/worker.py | head -20
```

Expected: lines showing the import, the helper definitions, and the call site.

- [ ] **Step 4: Verify TaskSpec v3 discriminated from QueuePayload**

```bash
cd /Users/wy/projects/openbot && uv run python -c "
from openbot.infrastructure.queue.task_spec import deserialize_task_spec
import json
# v3 spec accepted
blob = json.dumps({'spec_version': 3, 'task_id': 't1', 'run_id': 'r1', 'prev_run_id': None,
    'resource_key': None, 'event_seq': 0, 'intent': 'start',
    'enqueued_at': '2026-01-01T00:00:00+00:00', 'spec_built_at': '2026-01-01T00:00:00+00:00',
    'scenario': 'triage', 'channel': 'github', 'delivery_id': 'del-1',
    'kind': 'ISSUE_OPENED', 'repo': 'org/repo', 'actor': 'alice',
    'actor_type': None, 'issue_number': 1, 'pr_number': None,
    'comment_body': None, 'installation_id': 42, 'raw': {},
    'check_run_id': None, 'decision_trace': [], 'classifier_skipped': True,
    'stages_to_run': [], 'initial_labels': []})
spec = deserialize_task_spec(blob)
assert spec is not None and spec.spec_version == 3, 'v3 must parse'
# v2 payload rejected
assert deserialize_task_spec(json.dumps({'version': 2, 'task_id': 'x'})) is None, 'v2 must be rejected'
print('OK: v3 parses, v2 rejected')
"
```

Expected: `OK: v3 parses, v2 rejected`

- [ ] **Step 5: Tag completion and note spec amendment**

The F1 spec (§8) lists this acceptance criterion not yet addressed by the code:

> "Decision trace captured per middleware (latency_ms, outcome, reason)"

This is intentionally deferred to F3 (LLM classifier + incremental review slice). For F1, `decision_trace=[]` is the correct and expected value. The `classifier_skipped=True` field signals this explicitly to any consumer reading the TaskSpec.

- [ ] **Step 6: Final commit**

```bash
cd /Users/wy/projects/openbot && git log --oneline -8
```

Expected (newest first):
```
<hash> test(f-series): add F-01..F-05 acceptance tests for F1 slice
<hash> feat(worker): add v3 TaskSpec routing path (W1-W8)
<hash> feat(webhook): wire BackgroundTask to decide_and_enqueue() (F1)
<hash> feat(dispatcher): add decide_and_enqueue() webhook async segment
<hash> feat(dispatcher): add execute_handler() for TaskSpec v3 worker path
<hash> feat(queue): add enqueue_task_spec() to QueuePort, RedisStreamQueue, FakeQueue
<hash> feat(queue): add TaskSpec v3 schema and deserializer
```

If all 7 commits are present and `make check` is green, F1 is complete.

---

**F1 slice done.** The webhook async segment now owns all preflight decisions. Workers receive a pre-decided `TaskSpec v3` and execute handlers without re-running any preflight gates. Legacy `QueuePayload` v1/v2 entries continue through the unchanged path.

**Next:** F2 slice — direct-action events + context enrichment (see design spec §8 F2).
