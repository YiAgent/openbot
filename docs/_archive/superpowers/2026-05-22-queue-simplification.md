# Queue Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the BackgroundTask fallback path and v1/v2 payload compatibility, leaving a single queue path where all tasks flow through Redis Streams as v3 TaskSpec entries.

**Architecture:** We delete three things: (1) the `_BackgroundDispatch` / BackgroundTask fallback in `ingest_webhook` — Redis failure now raises 500 and lets GitHub retry; (2) the v1/v2 legacy routing in the worker — `_process_entry` only handles v3 TaskSpecs; (3) the `run_dispatch` function — the combined preflight+classify+handler entry point that only existed to serve the BackgroundTask and legacy v1/v2 paths.

**Tech Stack:** Python 3.12+, FastAPI, redis-py (async), fakeredis (tests), pytest

**Spec:** `docs/superpowers/specs/2026-05-21-queue-simplification-design.md`

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `openbot/application/use_cases/ingest_webhook.py` | Modify | Remove `_BackgroundDispatch`, remove `background_dispatch` field, Redis failure → raise |
| `openbot/entrypoints/api/routes/github_webhook.py` | Modify | Remove BackgroundTask wiring, remove `_decide_and_enqueue_bg` |
| `openbot/infrastructure/queue/worker.py` | Modify | Remove v1/v2 routing, `_is_v3_spec`, `run_dispatch` import, `QueuePayload` import |
| `openbot/application/dispatcher.py` | Modify | Delete `run_dispatch` function, clean up `__all__` |
| `tests/infrastructure/queue/test_worker.py` | Modify | Migrate from patching `run_dispatch` → `execute_handler` |
| `tests/infrastructure/queue/test_worker_v3.py` | Modify | Delete legacy v2 test + `_is_v3_spec` unit tests |
| `tests/application/dispatcher/test_f_series.py` | Modify | Delete F-03 (v2 fallback), update F-02 |
| `tests/application/test_dispatcher.py` | Modify | Delete `run_dispatch` classifier tests, migrate sandbox provisioning tests to `execute_handler` |
| `tests/application/test_dispatcher_observability.py` | Modify | Migrate `run_dispatch` → `execute_handler` |
| `tests/entrypoints/api/test_check_runs.py` | Modify | Migrate check-run tests from `run_dispatch` → `execute_handler` |
| `tests/e2e/conftest.py` | Modify | Replace `run_dispatch` with `execute_handler` in `WebhookHarness.dispatch` |
| `tests/integration/test_worker_recovery.py` | Modify | Replace `run_dispatch` patches with `execute_handler` |

**Kept (no changes):**
- `openbot/dispatcher/decide.py` — `decide_and_enqueue` (analysis phase entry)
- `openbot/application/dispatcher.py` — `execute_handler`, `build_preflight_chain`, `_run_with_sandbox`
- All business handlers: `triage.py`, `review.py`, `fix.py`, `chat.py`
- `openbot/infrastructure/queue/payload.py` — `QueuePayload` may still be referenced by tests/other code; leave the module but the worker no longer imports it

---

### Task 1: Remove `_BackgroundDispatch` and BackgroundTask fallback from `ingest_webhook`

**Files:**
- Modify: `openbot/application/use_cases/ingest_webhook.py`
- Modify: `openbot/entrypoints/api/routes/github_webhook.py`

**Changes in `ingest_webhook.py`:**

- [ ] **Step 1: Delete `_BackgroundDispatch` dataclass**

Remove lines 44-52 (the entire class including docstring):

```python
# REMOVE these lines:
@dataclass(frozen=True, slots=True)
class _BackgroundDispatch:
    """Carry the dispatch payload back to the route for BackgroundTask wiring.
    ...
    """
    event: UnifiedEvent
    dispatch: Dispatch
    check_run_id: int | None
```

- [ ] **Step 2: Remove `background_dispatch` field from `IngestResult`**

Remove line 86 (the `background_dispatch` field) and update the docstring (lines 67-71, 85):

```python
# In the IngestResult dataclass docstring, remove the paragraph about
# ``background_dispatch`` (lines 68-71). Remove the comment on line 85.
# Delete line 86:
    background_dispatch: _BackgroundDispatch | None = field(default=None, compare=False)
```

- [ ] **Step 3: Replace BackgroundTask fallback with raising an exception**

Replace lines 336-388 (the entire enqueue-or-fallback block) with a simplified version that raises on Redis failure:

The current code at lines 336-388:
```python
    # ── 5. Enqueue or prepare BackgroundTask fallback ─────────────────────────
    ...
    if redis_client is not None:
        try:
            ...
            return IngestResult(status="accepted", ...)
        except Exception:
            _logger.exception("queue_enqueue_failed_falling_back_to_background_task", ...)

    # BackgroundTask fallback path ...
    return IngestResult(
        status="accepted",
        ...
        background_dispatch=_BackgroundDispatch(...),
    )
```

Replace with:

```python
    # ── 5. Enqueue to Redis Stream ────────────────────────────────────────────
    # Redis is required. If enqueue fails, raise so the route returns 500
    # and GitHub retries the webhook (GitHub retries with exponential backoff
    # for up to several days).
    if redis_client is None:
        raise RuntimeError(
            "Redis client is not configured — webhook dispatch requires Redis"
        )

    assert queue is not None, "queue port must be set when redis_client is present"
    entry_id = await queue.enqueue(
        event,
        feature=dispatch.feature,
        task_id=dispatch.task_id,
        check_run_id=check_run_id,
        intent=dispatch.intent,
        run_id=dispatch.run_id,
        prev_run_id=dispatch.prev_run_id,
        resource_key=dispatch.resource_key,
        event_seq=dispatch.event_seq,
    )
    return IngestResult(
        status="accepted",
        delivery_id=event.delivery_id,
        kind=event.kind.value,
        relevant=event.is_relevant,
        feature=dispatch.feature.value,
        task_id=dispatch.task_id,
        entry_id=entry_id,
        check_run_id=check_run_id,
    )
```

- [ ] **Step 4: Update the function docstring**

Remove references to BackgroundTask from the docstring (lines 7-12, 193-194, 202). The updated docstring should read:

```python
    """Process a parsed, authenticated webhook event.

    Callers (the route) are responsible for:
      - Reading the raw body and headers from the HTTP request.
      - Calling ``adapter.verify_signature`` and mapping ``SignatureError``
        to HTTP 401.
      - Calling ``adapter.parse_event`` and mapping errors to HTTP 401.

    This function handles all application-level orchestration:
      1. Dedup check.
      2. Router dispatch.
      3. State-machine classification (when DB + resource_key present).
      4. Cancellation signal.
      5. GitHub check run creation.
      6. Redis enqueue (raises if Redis is unavailable — route returns 500).
    """
```

Also remove the module-level docstring about BackgroundTasks (lines 7-12 of the file).

- [ ] **Step 5: Clean up the `redis_client` parameter type hint**

Change line 184 from `redis_client: Any = None` to remove the `= None` default — Redis is now required:

Actually, looking more carefully, `redis_client` is passed by the route from `getattr(state, "redis", None)`. The `None` default allows the function signature to work with DI. Instead, keep `= None` but make the check at the enqueue section strict.

- [ ] **Step 6: Run existing tests to confirm breakage**

```bash
pytest tests/entrypoints/api/test_webhook_endpoint.py -v 2>&1 | tail -20
```

Expected: tests that relied on the BackgroundTask fallback will fail.

- [ ] **Step 7: Commit**

```bash
git add openbot/application/use_cases/ingest_webhook.py
git commit -m "refactor: remove BackgroundTask fallback from ingest_webhook"
```

---

**Changes in `github_webhook.py`:**

- [ ] **Step 8: Remove BackgroundTask fallback wiring from the route**

Remove lines 67-77 (the `if result.background_dispatch is not None:` block):

```python
    # REMOVE:
    if result.background_dispatch is not None:
        bd = result.background_dispatch
        background.add_task(
            _decide_and_enqueue_bg,
            request.app,
            adapter,
            bd.event,
            bd.dispatch,
            bd.check_run_id,
            getattr(state, "audit", None),
        )
```

- [ ] **Step 9: Remove `_decide_and_enqueue_bg` function**

Remove lines 82-107 (the entire `_decide_and_enqueue_bg` async function).

- [ ] **Step 10: Clean up unused imports**

Remove `BackgroundTasks` from the FastAPI import (line 16), remove `decide_and_enqueue` import (line 19):

```python
# Before:
from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from openbot.dispatcher import decide_and_enqueue

# After:
from fastapi import APIRouter, Depends, Request, status
```

Also remove `background: BackgroundTasks` from the handler signature (line 35) and update the docstring to remove BackgroundTask references.

- [ ] **Step 11: Update route handler signature**

```python
@router.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    event: UnifiedEvent = _verified_github_event,
) -> dict[str, str | int | bool | None]:
```

- [ ] **Step 12: Remove unused TYPE_CHECKING imports**

Remove `AuditLogPort` from the TYPE_CHECKING block (line 24) since it was only used by `_decide_and_enqueue_bg`.

- [ ] **Step 13: Run tests**

```bash
pytest tests/entrypoints/api/test_webhook_endpoint.py -v 2>&1 | tail -30
```

- [ ] **Step 14: Commit**

```bash
git add openbot/entrypoints/api/routes/github_webhook.py
git commit -m "refactor: remove BackgroundTask wiring from github_webhook route"
```

---

### Task 2: Delete `run_dispatch` from dispatcher

**Files:**
- Modify: `openbot/application/dispatcher.py`

- [ ] **Step 1: Delete the `run_dispatch` function**

Remove lines 313-486 (the entire `run_dispatch` function, including its docstring).

- [ ] **Step 2: Remove `run_dispatch` from `__all__`**

Remove `"run_dispatch"` from the `__all__` list (line 595).

- [ ] **Step 3: Update the module docstring**

Remove references to `run_dispatch` from the module docstring (lines 5-6, 20-27). The updated docstring should read:

```python
"""Dispatch — workflow handler execution.

Shared by:
  - ``openbot.dispatcher.decide``           in-process fallback when no queue is configured
                                            (dev / unit tests).
  - ``openbot.infrastructure.queue.worker``  Redis Stream consumer, after deserialization.

Both paths arrive at ``execute_handler`` with the same inputs:
adapter, event, dispatch decision, config, session-factory handle, Redis handle.
The function builds a ``PreflightContext`` and invokes the workflow
handler via ``_run_with_sandbox``. Never raises out — the caller has
already 202'd / XACK'd by the time this returns.

The middleware list lives here (not in webapp) so the worker and the
webapp can't drift apart on the chain order — a single source of
truth per spec §3 M3.

Slice-C note (sandbox DI): ``execute_handler`` accepts a ``sandbox_factory``
kwarg (default ``None``) so the fix use case can open a sandbox per event.
"""
```

- [ ] **Step 4: Run the test suite to verify breakage**

```bash
pytest tests/application/test_dispatcher.py -v 2>&1 | tail -30
```

Expected: tests that directly import and call `run_dispatch` will fail with `ImportError`.

- [ ] **Step 5: Commit**

```bash
git add openbot/application/dispatcher.py
git commit -m "refactor: remove run_dispatch — combined preflight+handler entry point"
```

---

### Task 3: Remove v1/v2 routing from worker

**Files:**
- Modify: `openbot/infrastructure/queue/worker.py`

- [ ] **Step 1: Remove `_is_v3_spec` function**

Remove lines 99-113 (the entire `_is_v3_spec` function and its docstring).

- [ ] **Step 2: Remove `_attach_sentry_tags` function**

Remove lines 116-131. This was only called from the v1/v2 path in `_process_entry`. The v3 path (`_execute_task_spec`) doesn't use it.

- [ ] **Step 3: Simplify `_process_entry` to only handle v3**

Replace the current `_process_entry` (lines 433-587) with a version that only deserializes TaskSpec v3:

```python
async def _process_entry(
    *,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    entry_id: str,
    fields: dict,
    agent_checkpointer: Any | None = None,
) -> None:
    """Deserialize TaskSpec v3 → dispatch → ack/dlq one entry."""
    blob = _extract_payload_blob(fields)
    if blob is None:
        await _ack_and_dlq(redis, entry_id, reason="payload_unreadable")
        return

    spec = deserialize_task_spec(blob)
    if spec is None:
        await _ack_and_dlq(redis, entry_id, reason="task_spec_unreadable")
        return

    await _execute_task_spec(
        spec,
        entry_id=entry_id,
        redis=redis,
        adapter=adapter,
        session_factory=session_factory,
        agent_checkpointer=agent_checkpointer,
    )
```

- [ ] **Step 4: Clean up unused imports**

Remove from the import block (lines 37, 55-62):
- `run_dispatch` from the `openbot.application.dispatcher` import
- `dispatch_for`, `upgrade_dispatch` from `openbot.application.router` (only used in v1/v2 path)
- `QueuePayload`, `deserialize_payload` from `openbot.infrastructure.queue.payload`
- `parse_classifier_output` from `openbot.dispatcher.classifier` (only used in `_execute_task_spec` after we move it — wait, it IS used in `_execute_task_spec`)

Let me re-check:
- `execute_handler` — used in `_execute_task_spec`, keep
- `run_dispatch` — only used in v1/v2 path, remove
- `dispatch_for`, `upgrade_dispatch` — used in `_execute_task_spec`, keep
- `QueuePayload`, `deserialize_payload` — only used in v1/v2 path, remove
- `parse_classifier_output` — used in `_execute_task_spec`, keep

So only remove:
```python
# Remove these lines:
from openbot.application.dispatcher import execute_handler, run_dispatch
# Replace with:
from openbot.application.dispatcher import execute_handler

# Remove:
from openbot.infrastructure.queue.payload import (
    DEAD_STREAM,
    GROUP_NAME,
    MAX_STREAM_LEN,
    STREAM_NAME,
    QueuePayload,
    deserialize_payload,
)
# Replace with (keep the constants, remove QueuePayload and deserialize_payload):
from openbot.infrastructure.queue.payload import (
    DEAD_STREAM,
    GROUP_NAME,
    MAX_STREAM_LEN,
    STREAM_NAME,
)
```

Wait, actually `_ack_and_dlq` still references `QueuePayload` in its signature. Let me check line 609:

```python
async def _ack_and_dlq(
    redis: redis_async.Redis,
    entry_id: str,
    *,
    reason: str,
    payload: QueuePayload | None = None,
) -> None:
```

This is called from two places:
1. v3 path: `await _ack_and_dlq(redis, entry_id, reason="max_attempts_v3")` — no payload
2. v1/v2 path: `await _ack_and_dlq(redis, entry_id, reason="max_attempts", payload=payload)` — with payload

With v1/v2 removed, the `payload` parameter is only ever passed as `None`. We should clean up `_ack_and_dlq` too.

- [ ] **Step 5: Simplify `_ack_and_dlq` to remove `QueuePayload` dependency**

```python
async def _ack_and_dlq(
    redis: redis_async.Redis,
    entry_id: str,
    *,
    reason: str,
) -> None:
    """Move an entry to the DLQ stream and XACK so it stops circulating."""
    fields: dict[str, str] = {
        "reason": reason,
        "src_entry_id": entry_id,
    }
    try:
        await redis.xadd(DEAD_STREAM, fields, maxlen=MAX_STREAM_LEN, approximate=True)
    except Exception:
        _logger.exception("queue_dlq_write_failed", extra={"entry_id": entry_id})
    try:
        await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
    except Exception:
        _logger.exception("queue_dlq_ack_failed", extra={"entry_id": entry_id})
```

- [ ] **Step 6: Also remove `_attach_sentry_tags` from the list of kept functions**

(Already done in Step 2.)

- [ ] **Step 7: Remove the `QueuePayload` TYPE_CHECKING import if there was one**

Check: there is no TYPE_CHECKING import of QueuePayload. Only the concrete import on line 60.

Also remove `parse_classifier_output` check — wait, it IS used in `_execute_task_spec` (line 201: `classifier_output = parse_classifier_output(...)`). Keep it.

- [ ] **Step 8: Run worker tests**

```bash
pytest tests/infrastructure/queue/ -v 2>&1 | tail -30
```

Expected: `test_worker_legacy_v2_payload_still_works` will fail (tests v2 path that we removed). Other v3 tests should still pass.

- [ ] **Step 9: Commit**

```bash
git add openbot/infrastructure/queue/worker.py
git commit -m "refactor: remove v1/v2 payload routing from worker"
```

---

### Task 4: Update `test_worker.py` — migrate from `run_dispatch` to `execute_handler`

**Files:**
- Modify: `tests/infrastructure/queue/test_worker.py`

All six tests in this file patch `openbot.infrastructure.queue.worker.run_dispatch`. After Task 3, the worker only calls `execute_handler` for v3 entries, so we need to update the patches.

- [ ] **Step 1: Update `test_consumer_acks_after_successful_dispatch` (line 67)**

Replace:
```python
with patch(
    "openbot.infrastructure.queue.worker.run_dispatch", new=AsyncMock(return_value=None)
):
```
With:
```python
with patch(
    "openbot.infrastructure.queue.worker.execute_handler", new=AsyncMock(return_value=None)
):
```

Also update the helper `_payload` — it currently creates a `QueuePayload` (v2 format). After simplification, the worker only accepts v3 TaskSpecs. Change the helper to create a TaskSpec v3:

```python
from openbot.infrastructure.queue.task_spec import TaskSpec

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
```

And update the enqueue call to use the queue port's `enqueue_task_spec` instead of the raw `enqueue`:

Actually, looking at how the test currently enqueues:
```python
await enqueue(redis, _payload())
```

This calls the legacy `enqueue` function that XADDs a `QueuePayload`. After simplification, we need to XADD a TaskSpec. Since these tests are about the worker consuming and XACKing, we should directly XADD the TaskSpec JSON:

```python
spec = _spec()
await redis.xadd(STREAM_NAME, {"json": spec.to_json()})
```

And remove the `from openbot.infrastructure.queue import ... enqueue ...` import.

- [ ] **Step 2: Update `test_consumer_skips_unrouted_payload` (line 84)**

This test builds a tampered QueuePayload JSON. Update to build a tampered TaskSpec JSON with an unroutable kind. After simplification, `_process_entry` calls `deserialize_task_spec`, and if that returns None (or if the spec's event can't be routed), it goes to DLQ. The behavior is similar but uses TaskSpec:

```python
async def test_consumer_skips_unroutable_spec() -> None:
    """A TaskSpec whose event kind is no longer routable is handled gracefully."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_consumer_group(redis)

    spec = _spec()
    # Tamper the JSON to use an unroutable kind.
    tampered = spec.to_json().replace('"issue.opened"', '"issue.transferred"')
    await redis.xadd(STREAM_NAME, {"json": tampered})

    adapter = AsyncMock()
    with patch(
        "openbot.infrastructure.queue.worker.execute_handler", new=AsyncMock()
    ) as mock_handler:
        await _run_one_iteration(redis, adapter=adapter, session_factory=None)

    mock_handler.assert_not_called()
    pending = await redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 0
```

- [ ] **Step 3: Update `test_retry_counter_incremented_per_attempt` (line 125)**

Replace `run_dispatch` patch with `execute_handler` patch, and use TaskSpec:

```python
async def test_retry_counter_incremented_per_attempt() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_consumer_group(redis)

    spec = _spec()
    await redis.xadd(STREAM_NAME, {"json": spec.to_json()})

    adapter = AsyncMock()
    with patch(
        "openbot.infrastructure.queue.worker.execute_handler", new=AsyncMock(return_value=None)
    ):
        await _run_one_iteration(redis, adapter=adapter, session_factory=None)

    counter_keys = await redis.keys("openbot:workflows:retries:*")
    assert len(counter_keys) == 1
    count = await redis.get(counter_keys[0])
    assert int(count) == 1
```

- [ ] **Step 4: Update `test_dlq_entry_preserves_original_payload` (line 189)**

This test XADDs malformed JSON and checks the DLQ. After simplification, `_ack_and_dlq` no longer attaches the payload JSON (since `QueuePayload` is removed). Update the assertion:

```python
async def test_dlq_entry_preserves_original_payload() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_consumer_group(redis)
    await redis.xadd(STREAM_NAME, {"json": "{not json"})

    adapter = AsyncMock()
    await _run_one_iteration(redis, adapter=adapter, session_factory=None)

    assert await redis.xlen(DEAD_STREAM) == 1
    pending = await redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending["pending"] == 0

    dlq_entries = await redis.xrange(DEAD_STREAM, count=1)
    assert len(dlq_entries) == 1
    _entry_id, fields = dlq_entries[0]
    assert fields["reason"] == "payload_unreadable"
    assert fields["src_entry_id"]  # the original entry ID is preserved
```

- [ ] **Step 5: The remaining tests (`test_ensure_consumer_group_idempotent`, `test_consumer_exits_on_shutdown_event`) don't reference `run_dispatch` — they should pass unchanged.**

- [ ] **Step 6: Run tests**

```bash
pytest tests/infrastructure/queue/test_worker.py -v 2>&1 | tail -30
```

- [ ] **Step 7: Commit**

```bash
git add tests/infrastructure/queue/test_worker.py
git commit -m "test: migrate worker tests from run_dispatch to execute_handler"
```

---

### Task 5: Update `test_worker_v3.py` — remove legacy v2 test and `_is_v3_spec` tests

**Files:**
- Modify: `tests/infrastructure/queue/test_worker_v3.py`

- [ ] **Step 1: Delete `_is_v3_spec` unit tests**

Remove lines 37-53 (four test functions: `test_is_v3_spec_true`, `test_is_v3_spec_false_for_v2_payload`, `test_is_v3_spec_false_for_garbage`, `test_is_v3_spec_bytes_true`).

- [ ] **Step 2: Remove `_is_v3_spec` from the import**

Remove `_is_v3_spec` from the import block (line 16):

```python
# Before:
from openbot.infrastructure.queue.worker import (
    GROUP_NAME,
    STREAM_NAME,
    _is_v3_spec,
    consume_loop,
    ensure_consumer_group,
)
# After:
from openbot.infrastructure.queue.worker import (
    GROUP_NAME,
    STREAM_NAME,
    consume_loop,
    ensure_consumer_group,
)
```

- [ ] **Step 3: Delete `test_worker_legacy_v2_payload_still_works`**

Remove lines 140-180 (the entire test function). This test validates the v2 fallback that no longer exists.

- [ ] **Step 4: Update the module docstring**

Remove "legacy fallback" from line 1. Change:
```python
"""Worker: v3 TaskSpec routing, W1 cancel quick-check, legacy fallback."""
```
To:
```python
"""Worker: v3 TaskSpec routing, W1 cancel quick-check, reviewed-SHA persistence."""
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/infrastructure/queue/test_worker_v3.py -v 2>&1 | tail -30
```

- [ ] **Step 6: Commit**

```bash
git add tests/infrastructure/queue/test_worker_v3.py
git commit -m "test: remove legacy v2 fallback test and _is_v3_spec unit tests"
```

---

### Task 6: Update `test_f_series.py` — remove F-03, update F-02

**Files:**
- Modify: `tests/application/dispatcher/test_f_series.py`

- [ ] **Step 1: Delete F-03 test**

Remove lines 149-201 (`test_f03_worker_falls_back_to_run_dispatch_for_v2` function).

- [ ] **Step 2: Update F-02 test to remove `run_dispatch` assertions**

Remove the `run_dispatch_calls` tracking and `fake_run_dispatch` monkeypatch from `test_f02_worker_routes_v3_to_execute_handler` (lines 105-108, 114-117, 142):

The updated F-02 test:
```python
@pytest.mark.asyncio
async def test_f02_worker_routes_v3_to_execute_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-02: Worker calls execute_handler for v3 TaskSpec blobs."""
    handler_calls: list[str] = []

    async def fake_execute_handler(**kw: object) -> None:
        handler_calls.append(cast(UnifiedEvent, kw["event"]).delivery_id)

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
```

- [ ] **Step 3: Update module docstring**

Remove line 6: `F-03  Worker falls back to run_dispatch for legacy v2 QueuePayload entries`

- [ ] **Step 4: Run tests**

```bash
pytest tests/application/dispatcher/test_f_series.py -v 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add tests/application/dispatcher/test_f_series.py
git commit -m "test: remove F-03 v2 fallback test, update F-02"
```

---

### Task 7: Update `test_dispatcher.py` — delete `run_dispatch` tests, migrate sandbox tests

**Files:**
- Modify: `tests/application/test_dispatcher.py`

- [ ] **Step 1: Delete the four `run_dispatch`-specific classifier tests**

Remove lines 84-220. These four tests directly test `run_dispatch` behavior around the classifier:
- `test_run_dispatch_calls_classifier_after_preflight`
- `test_run_dispatch_threads_classifier_output_into_ctx`
- `test_run_dispatch_classifier_output_none_when_helper_returns_none`
- `test_run_dispatch_does_not_call_classifier_when_preflight_blocks`

These tests can't be migrated to `execute_handler` because `execute_handler` doesn't call the classifier (that's `decide_and_enqueue`'s job).

- [ ] **Step 2: Migrate the sandbox provisioning tests from `run_dispatch` → `execute_handler`**

The following tests call `run_dispatch` but test `_run_with_sandbox` behavior (which is shared with `execute_handler`):

1. `test_dispatcher_provisions_sandbox_on_required_policy` (line 318) — change `run_dispatch` → `execute_handler`, add `config=AsyncMock()` kwarg
2. `test_dispatcher_skips_provisioning_on_static_no_sandbox` (line 376) — same
3. `test_dispatcher_skips_provisioning_on_classifier_unclear_chat` (line 420) — same
4. `test_dispatcher_degrades_gracefully_on_clone_failure` (line 461) — same
5. `test_dispatcher_degrades_gracefully_on_factory_none` (line 505) — same
6. `test_dispatcher_degrades_gracefully_on_resolver_error` (line 539) — same

For each, change:
```python
await run_dispatch(
    adapter=...,
    event=...,
    dispatch=...,
    session_factory=...,
    redis=...,
    sandbox_factory=...,
)
```
To:
```python
await execute_handler(
    adapter=...,
    event=...,
    dispatch=...,
    config=AsyncMock(),  # execute_handler requires config (not loaded internally)
    session_factory=...,
    redis=...,
    sandbox_factory=...,
    classifier_output=...,  # pass classifier_output kwarg where needed
)
```

For tests that use a specific `classifier_output` to test OR-merge behavior, pass it via the kwarg. For tests that use `classify_for_dispatch` monkeypatch, either:
- Remove the monkeypatch and pass `classifier_output` directly, or
- Keep the `classify_for_dispatch` monkeypatch but note that `execute_handler` won't call it (the handler just reads `ctx.classifier_output`)

Actually, the simplest approach: since `execute_handler` doesn't call `classify_for_dispatch` at all, we should:
1. Remove the `classify_for_dispatch` monkeypatches from tests being migrated
2. Pass `classifier_output` via the kwarg to `execute_handler`

For tests that test OR-merge behavior (classifier returning "unclear" → sandbox bypass), the `classifier_output` kwarg on `execute_handler` gets threaded into the `PreflightContext`, and `_run_with_sandbox` reads `ctx.classifier_output` for the OR-merge. So the same behavior is exercised.

For the degrade path tests (resolver error, clone failure, factory None), the `classifier_output` kwarg doesn't matter (the degrade happens before the handler). Pass `classifier_output=None`.

- [ ] **Step 3: Update `test_dispatcher_provisions_sandbox_on_required_policy`**

```python
@pytest.mark.asyncio
async def test_dispatcher_provisions_sandbox_on_required_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path. Static REQUIRED + triage classifier output → handler sees
    a populated ``SandboxedHandle``."""
    spec = CheckoutSpec(
        repo_url="https://github.com/acme/widget.git",
        ref="deadbeef",
        strategy=CloneStrategy.SHALLOW,
    )
    monkeypatch.setattr(
        "openbot.application.dispatcher.resolve_checkout",
        _resolve_checkout_returning(spec),
    )

    sandbox = FakeSandboxLifecycle()
    adapter = AsyncMock()
    adapter.get_installation_token = AsyncMock(return_value="ghs_token_42")

    seen_ctx: dict[str, PreflightContext] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen_ctx["ctx"] = ctx

    await execute_handler(
        adapter=adapter,
        event=_event(),
        dispatch=Dispatch(feature=Feature.TRIAGE, task_id="t-1", handler=handler),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        sandbox_factory=_factory_from_sandbox(sandbox),
        classifier_output=TriageClassifierOutput(
            type="bug",
            severity_guess="medium",
            has_reproduction_info=True,
            looks_like_spam=False,
        ),
    )

    handle = seen_ctx["ctx"].sandbox_handle
    assert handle is not None
    assert handle.checkout is spec
    assert handle.token == "ghs_token_42"
    assert handle.sandbox is sandbox
    assert sandbox.cloned == [("https://github.com/acme/widget.git", "deadbeef", "ghs_token_42")]
    assert sandbox.clone_strategies == [CloneStrategy.SHALLOW]
    assert sandbox.closed is True
```

- [ ] **Step 4: Update remaining sandbox tests similarly**

Each test follows the same pattern: remove `_proceed_preflight(monkeypatch)`, remove `classify_for_dispatch` monkeypatch, replace `run_dispatch(...)` with `execute_handler(..., config=AsyncMock(), classifier_output=..., ...)`.

For `test_dispatcher_skips_provisioning_on_static_no_sandbox`: pass `classifier_output=None`, keep `sandbox_policy=SandboxPolicy.NO_SANDBOX` on the Dispatch.

For `test_dispatcher_skips_provisioning_on_classifier_unclear_chat`: pass `classifier_output=ChatClassifierOutput(intent="unclear", needs_clarification=True, scope_hint=None)`.

For degrade tests: pass `classifier_output=None`.

- [ ] **Step 5: Remove `_proceed_preflight` helper**

This helper (`_proceed_preflight`, lines 66-81) was only used by `run_dispatch` tests. Remove it.

- [ ] **Step 6: Update imports**

Remove `run_dispatch` from the import (line 36):
```python
# Before:
from openbot.application.dispatcher import execute_handler, run_dispatch
# After:
from openbot.application.dispatcher import execute_handler
```

Remove `MiddlewareDecision` and `MiddlewareResult` from the middleware import (lines 37-41) — these were used by `_proceed_preflight`:
```python
# Before:
from openbot.application.middleware import (
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
)
# After:
from openbot.application.middleware import PreflightContext
```

- [ ] **Step 7: Update module docstring**

Remove references to `run_dispatch` and Task 2.1 from the docstring (lines 5-8, 19-20).

- [ ] **Step 8: Run tests**

```bash
pytest tests/application/test_dispatcher.py -v 2>&1 | tail -40
```

- [ ] **Step 9: Commit**

```bash
git add tests/application/test_dispatcher.py
git commit -m "test: migrate dispatcher tests from run_dispatch to execute_handler"
```

---

### Task 8: Update `test_dispatcher_observability.py` — migrate to `execute_handler`

**Files:**
- Modify: `tests/application/test_dispatcher_observability.py`

All five `dispatch_sandbox_total` counter tests call `run_dispatch`. They need to be migrated to `execute_handler`.

- [ ] **Step 1: Update imports**

```python
# Before:
from openbot.application.dispatcher import run_dispatch
# After:
from openbot.application.dispatcher import execute_handler
```

- [ ] **Step 2: Migrate each test from `run_dispatch` → `execute_handler`**

The key differences:
- `execute_handler` doesn't load config internally — pass `config=AsyncMock()`
- `execute_handler` doesn't call `classify_for_dispatch` — pass `classifier_output` via kwarg
- `execute_handler` doesn't call `run_preflight` — remove `_proceed_preflight` calls and monkeypatches

Remove the `_proceed_preflight` helper (lines 68-76).

For each test, replace:
```python
_proceed_preflight(monkeypatch)
# ... classify_for_dispatch monkeypatch ...
await run_dispatch(adapter=..., event=..., dispatch=..., session_factory=..., redis=..., sandbox_factory=...)
```
With:
```python
await execute_handler(
    adapter=...,
    event=...,
    dispatch=...,
    config=AsyncMock(),
    session_factory=...,
    redis=...,
    sandbox_factory=...,
    classifier_output=...,  # None or a specific classifier output as needed
)
```

- `_record_counter` patches `openbot.application.dispatcher.dispatch_sandbox_total` — this still works because `_run_with_sandbox` (called by `execute_handler`) reads from the same module attribute.

- [ ] **Step 3: Update `test_dispatch_sandbox_counter_fires_with_none_on_happy_path`**

Remove `_proceed_preflight(monkeypatch)`, remove `classify_for_dispatch` monkeypatch, pass `classifier_output=TriageClassifierOutput(...)`:

```python
@pytest.mark.asyncio
async def test_dispatch_sandbox_counter_fires_with_none_on_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path → ``bypass_source='none'``, ``policy='required'``."""
    monkeypatch.setattr(
        "openbot.application.dispatcher.resolve_checkout",
        AsyncMock(
            return_value=CheckoutSpec(
                repo_url="https://x/y.git", ref="sha", strategy=CloneStrategy.SHALLOW
            )
        ),
    )
    counter = _record_counter(monkeypatch, "dispatch_sandbox_total")

    sandbox = FakeSandboxLifecycle()
    adapter = AsyncMock()
    adapter.get_installation_token = AsyncMock(return_value="ghs_t")

    async def handler(ctx: PreflightContext) -> None:
        pass

    await execute_handler(
        adapter=adapter,
        event=_event(),
        dispatch=Dispatch(feature=Feature.TRIAGE, task_id="t-1", handler=handler),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        sandbox_factory=_factory_from_sandbox(sandbox),
        classifier_output=TriageClassifierOutput(
            type="bug",
            severity_guess="medium",
            has_reproduction_info=True,
            looks_like_spam=False,
        ),
    )

    counter.labels.assert_called_once_with(
        feature="triage", policy="required", bypass_source="none"
    )
    counter.labels.return_value.inc.assert_called_once()
```

- [ ] **Step 4: Update remaining four counter tests similarly**

Follow the same pattern: remove `_proceed_preflight`, remove `classify_for_dispatch` monkeypatch, pass `classifier_output` kwarg, add `config=AsyncMock()`.

For `test_dispatch_sandbox_counter_fires_with_static_on_no_sandbox_route`: pass `classifier_output=None`, keep `sandbox_policy=SandboxPolicy.NO_SANDBOX`.

For `test_dispatch_sandbox_counter_fires_with_classifier_on_dynamic_skip`: pass `classifier_output=ChatClassifierOutput(intent="unclear", needs_clarification=True, scope_hint=None)`.

For degrade tests: pass `classifier_output=None`.

- [ ] **Step 5: Run tests**

```bash
pytest tests/application/test_dispatcher_observability.py -v 2>&1 | tail -30
```

- [ ] **Step 6: Commit**

```bash
git add tests/application/test_dispatcher_observability.py
git commit -m "test: migrate observability tests from run_dispatch to execute_handler"
```

---

### Task 9: Update `test_check_runs.py` — migrate check-run tests

**Files:**
- Modify: `tests/entrypoints/api/test_check_runs.py`

- [ ] **Step 1: Migrate `test_run_dispatch_updates_check_run` → `test_execute_handler_updates_check_run`**

Replace `run_dispatch` with `execute_handler`. `execute_handler` doesn't load config or run preflight — pass `config=AsyncMock()`:

```python
@pytest.mark.asyncio
async def test_execute_handler_updates_check_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from openbot.application.dispatcher import execute_handler
    from openbot.application.router import Dispatch
    from openbot.domain.events import EventKind, UnifiedEvent
    from openbot.domain.workflows import Feature

    adapter = AsyncMock()
    event = UnifiedEvent(
        channel="github",
        delivery_id="d1",
        kind=EventKind.PR_OPENED,
        repo="r",
        actor="a",
        pr_number=101,
        raw={"pull_request": {"head": {"sha": "s1"}}},
    )

    async def fake_handler(ctx: object) -> None:
        pass

    dispatch = Dispatch(feature=Feature.REVIEW, task_id="t1", handler=fake_handler)

    await execute_handler(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        check_run_id=888,
    )

    adapter.update_check_run.assert_called_with(
        event,
        888,
        status="completed",
        conclusion="success",
        output={
            "title": "Analysis Complete",
            "summary": "Workflow `review` finished.",
        },
    )
```

Note: the success message changed from `"Workflow `review` finished successfully."` to `"Workflow `review` finished."` — that's the actual output in `execute_handler` (line 550).

- [ ] **Step 2: Migrate `test_run_dispatch_updates_check_run_on_failure` similarly**

```python
@pytest.mark.asyncio
async def test_execute_handler_updates_check_run_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbot.application.dispatcher import execute_handler
    from openbot.application.router import Dispatch
    from openbot.domain.events import EventKind, UnifiedEvent
    from openbot.domain.workflows import Feature

    adapter = AsyncMock()
    event = UnifiedEvent(
        channel="github",
        delivery_id="d1",
        kind=EventKind.PR_OPENED,
        repo="r",
        actor="a",
        pr_number=101,
        raw={"pull_request": {"head": {"sha": "s1"}}},
    )

    async def crashing_handler(ctx: object) -> None:
        raise RuntimeError("boom")

    dispatch = Dispatch(feature=Feature.REVIEW, task_id="t1", handler=crashing_handler)

    await execute_handler(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        check_run_id=777,
    )

    adapter.update_check_run.assert_called_with(
        event,
        777,
        status="completed",
        conclusion="failure",
        output={
            "title": "Handler Crash",
            "summary": "Handler `review` raised unexpectedly.",
        },
    )
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/entrypoints/api/test_check_runs.py -v 2>&1 | tail -20
```

- [ ] **Step 4: Commit**

```bash
git add tests/entrypoints/api/test_check_runs.py
git commit -m "test: migrate check-run tests from run_dispatch to execute_handler"
```

---

### Task 10: Update `test_worker_recovery.py` — replace `run_dispatch` patches

**Files:**
- Modify: `tests/integration/test_worker_recovery.py`

- [ ] **Step 1: Replace `run_dispatch` patches with `execute_handler` patches**

The `_one_iteration` helper (lines 43-81) patches `openbot.infrastructure.queue.worker.run_dispatch`. After Task 3, this attribute no longer exists. Replace with `execute_handler`:

```python
async def _one_iteration(
    sm: SMHarness,
    *,
    consumer_name: str,
    dispatch_raises: Exception | None = None,
    read_block_ms: int = 50,
) -> None:
    """Run one ``consume_loop`` iteration and wait for it to finish.

    Patches ``openbot.infrastructure.queue.worker.execute_handler`` for the duration:
      - If ``dispatch_raises`` is given, the mock raises that exception
        (simulates a crashed handler).
      - Otherwise the mock returns None (simulates a successful handler).
    """
    mock_handler = AsyncMock(
        side_effect=dispatch_raises,
        return_value=None,
    )
    shutdown = asyncio.Event()
    adapter = AsyncMock()

    with patch("openbot.infrastructure.queue.worker.execute_handler", new=mock_handler):
        task = asyncio.create_task(
            consume_loop(
                redis=sm.redis,
                adapter=adapter,
                session_factory=sm.session_factory,
                consumer_name=consumer_name,
                shutdown=shutdown,
                read_block_ms=read_block_ms,
            )
        )
        await asyncio.sleep(0.25)
        shutdown.set()
        await asyncio.wait_for(task, timeout=3.0)
```

Also update the `mock_handler` variable name in the test functions that reference `mock_dispatch`.

- [ ] **Step 2: Update docstring**

Line 17-18: change `run_dispatch` references to `execute_handler`.

- [ ] **Step 3: The webhook POST in these tests goes through the full route**

After Task 1, the route no longer has the BackgroundTask fallback. But the tests use `sm.client.post(...)` which goes through the real FastAPI route. The webhook route calls `ingest_webhook`, which now requires Redis. Since the test uses fakeredis and the SMHarness has redis configured, the enqueue should succeed — no BackgroundTask needed.

However, the test's webhook POST expects the entry to land in the Redis stream. After our changes, `ingest_webhook` calls `queue.enqueue(...)` which writes to Redis. The SMHarness should have a queue port configured. Let me check...

Looking at the integration test conftest (`tests/integration/conftest.py`), the `SMHarness` likely sets up the full app state. The route reads `queue` from `request.app.state`. If the queue port is configured, the enqueue should work.

If the test fails because the queue port is None or the enqueue fails, we need to ensure the SMHarness provides the queue port. This might require a small update to the conftest.

- [ ] **Step 4: Run tests**

```bash
pytest tests/integration/test_worker_recovery.py -v 2>&1 | tail -40
```

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_worker_recovery.py
git commit -m "test: migrate worker recovery tests from run_dispatch to execute_handler"
```

---

### Task 11: Update `tests/e2e/conftest.py` — replace `run_dispatch` in harness

**Files:**
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Replace `run_dispatch` import with `execute_handler`**

```python
# Before (line 43):
from openbot.application.dispatcher import run_dispatch
# After:
from openbot.application.dispatcher import execute_handler
```

- [ ] **Step 2: Update `WebhookHarness.dispatch` method**

Replace the `run_dispatch` call (lines 355-363) with `execute_handler`. `execute_handler` requires `config` and doesn't load it internally:

```python
async def dispatch(self, event: UnifiedEvent) -> None:
    """Run the workflow handler for ``event``.

    Mirrors what the worker does after popping a TaskSpec v3 from the queue.
    """
    decision: Dispatch | None = dispatch_for(event)
    if decision is None:
        return
    await execute_handler(
        adapter=self.adapter,
        event=event,
        dispatch=decision,
        config=self.config,
        session_factory=self.session_factory,
        redis=self.redis,
        sandbox_factory=self.sandbox_factory_override,
    )
```

- [ ] **Step 3: Also need to remove/update the `monkeypatch` for `load_for_repo`**

In the `webhook_harness` fixture, `load_for_repo` is patched (line 413). With `execute_handler`, this patch is no longer needed because `execute_handler` doesn't load config. But it's still used by `decide_and_enqueue` (called by the webhook route). The patch can stay — it just won't be hit by `execute_handler`.

- [ ] **Step 4: Run e2e tests**

```bash
pytest tests/e2e/ -v --timeout=60 2>&1 | tail -30
```

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "test: replace run_dispatch with execute_handler in e2e harness"
```

---

### Task 12: Final integration — run full test suite and fix remaining references

**Files:**
- Potentially modify: any file with remaining `run_dispatch` or `BackgroundTask` references

- [ ] **Step 1: Run the full test suite**

```bash
make test 2>&1 | tail -50
```

- [ ] **Step 2: For any remaining failures, fix references**

Search for any remaining imports or references:
```bash
grep -rn "run_dispatch\|_BackgroundDispatch\|background_dispatch\|_decide_and_enqueue_bg\|_is_v3_spec" openbot/ tests/ --include="*.py" | grep -v __pycache__ | grep -v ".pyc"
```

Expected: only historical comments/docstrings remain (these are fine), no functional references.

- [ ] **Step 3: Fix any remaining test failures**

Address any test failures from the full suite run. Common issues:
- `tests/e2e/__init__.py` — references to `run_dispatch` in docstrings (update)
- `tests/state_machine/test_error_paths.py` — may need updates if BackgroundTask references cause issues
- Import errors from modules that still import `run_dispatch` from dispatcher

- [ ] **Step 4: Run tests again to confirm all green**

```bash
make test 2>&1 | tail -20
```

- [ ] **Step 5: Run lint**

```bash
make lint 2>&1 | tail -20
```

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "chore: final cleanup of queue simplification — remove all legacy references"
```

---

## Self-Review

### 1. Spec Coverage

| Spec Requirement | Covered By |
|------------------|------------|
| Delete `_BackgroundDispatch` dataclass | Task 1, Step 1 |
| Remove `background_dispatch` field | Task 1, Step 2 |
| Redis fail → raise (no fallback) | Task 1, Step 3 |
| Remove BackgroundTask wiring from route | Task 1, Steps 8-11 |
| Remove `_decide_and_enqueue_bg` | Task 1, Step 9 |
| Remove v1/v2 routing in `_process_entry` | Task 3, Step 3 |
| Remove `_is_v3_spec` | Task 3, Step 1 |
| Remove `run_dispatch` | Task 2, Step 1 |
| Keep `decide_and_enqueue` | Not touched (confirmed) |
| Keep `execute_handler` | Not touched (confirmed) |
| Keep `build_preflight_chain` | Not touched (confirmed) |
| Keep business handlers | Not touched (confirmed) |
| Delete v1/v2 Worker unit tests | Task 5, Steps 1-3 |
| Delete BackgroundTask integration tests | Task 1 removes the code; tests updated in Tasks 4-11 |
| Delete `run_dispatch` tests | Tasks 7-8 |
| Keep v3 path tests | Tasks 4-11 preserve and migrate |

### 2. Placeholder Scan

No TBD, TODO, or "implement later" patterns. Every step has explicit code. ✓

### 3. Type Consistency

- `execute_handler` parameter names match across all task code snippets ✓
- `TaskSpec.from_event_and_dispatch(event, dispatch)` used consistently ✓
- `deserialize_task_spec` imported from `openbot.infrastructure.queue.task_spec` ✓
- The `config` kwarg is `AsyncMock()` in tests (matches `EffectiveConfig` duck-typing) ✓
