# Webhook-Worker Layering — F1: Chain Front-Migration (Part 1/2: Schema + Infra)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Part 2:** See `2026-05-20-webhook-worker-layering-f1-part2.md` for Tasks 4–8 (wire BackgroundTask, worker, tests).

**Goal:** Move the 10-middleware preflight chain from worker-side `run_dispatch()` to the webhook async segment (`decide_and_enqueue()`). Workers receive `TaskSpec v3` with pre-built decisions and execute handlers without re-running preflight.

**Architecture:** New `openbot/dispatcher/` package = webhook async segment that runs existing preflight chain then builds/enqueues `TaskSpec v3`. Worker detects `spec_version=3` and calls `execute_handler()` (handler-only). v1/v2 `QueuePayload` entries continue through the legacy path unchanged.

**Tech Stack:** Python 3.12, FastAPI BackgroundTasks, Redis Streams, fakeredis (tests)

**Spec:** `docs/specs/2026-05-17-webhook-worker-layering-design.md` §2/§3/§4/§8 F1

**Baseline:** `make check` → 705 tests pass.

---

## File Map (all tasks)

| Action | Path |
|--------|------|
| Create | `openbot/dispatcher/__init__.py` |
| Create | `openbot/dispatcher/decide.py` |
| Create | `openbot/infrastructure/queue/task_spec.py` |
| Modify | `openbot/application/dispatcher.py` — add `execute_handler()` |
| Modify | `openbot/application/ports/queue.py` — add `enqueue_task_spec()` |
| Modify | `openbot/infrastructure/queue/enqueue.py` — add `enqueue_task_spec()` |
| Modify | `openbot/entrypoints/api/routes/github_webhook.py` — BackgroundTask |
| Modify | `openbot/infrastructure/queue/worker.py` — v3 detect + route |
| Modify | `tests/_fakes/queue.py` — add `enqueue_task_spec()` |
| Create | `tests/application/dispatcher/__init__.py` |
| Create | `tests/application/dispatcher/test_decide.py` |
| Create | `tests/application/dispatcher/test_execute_handler.py` |
| Create | `tests/infrastructure/queue/test_task_spec.py` |
| Create | `tests/infrastructure/queue/test_worker_v3.py` |
| Create | `tests/application/dispatcher/test_f_series.py` |

---

## Task 1: TaskSpec v3 schema

**Files:**
- Create: `openbot/infrastructure/queue/task_spec.py`
- Create: `tests/infrastructure/queue/test_task_spec.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/infrastructure/queue/test_task_spec.py
"""TaskSpec v3 — schema, serialization, event reconstruction."""
from __future__ import annotations
import json
import pytest
from openbot.infrastructure.queue.task_spec import (
    TASK_SPEC_VERSION, DecisionStep, TaskSpec, deserialize_task_spec,
)


def _spec(**kw) -> TaskSpec:
    base = dict(
        spec_version=3, task_id="t-1", run_id="r-1", prev_run_id=None,
        resource_key="github:org/repo:issue:1", event_seq=0, intent="start",
        enqueued_at="2026-01-01T00:00:00+00:00",
        spec_built_at="2026-01-01T00:00:00+00:00",
        scenario="triage", channel="github", delivery_id="del-1",
        kind="ISSUE_OPENED", repo="org/repo", actor="alice",
        actor_type=None, issue_number=1, pr_number=None,
        comment_body=None, installation_id=42, raw={},
        check_run_id=None, decision_trace=[], classifier_skipped=True,
        stages_to_run=[], initial_labels=[],
    )
    base.update(kw)
    return TaskSpec(**base)  # type: ignore[arg-type]


def test_version_constant() -> None:
    assert TASK_SPEC_VERSION == 3


def test_round_trip() -> None:
    spec = _spec()
    blob = spec.to_json()
    assert json.loads(blob)["spec_version"] == 3
    restored = deserialize_task_spec(blob)
    assert restored is not None
    assert restored.task_id == "t-1"
    assert restored.classifier_skipped is True


def test_deserialize_bad_json_returns_none() -> None:
    assert deserialize_task_spec("not-json") is None


def test_deserialize_wrong_version_returns_none() -> None:
    assert deserialize_task_spec(json.dumps({"spec_version": 99})) is None


def test_v2_payload_rejected() -> None:
    """A v2 QueuePayload must NOT be accepted as TaskSpec."""
    assert deserialize_task_spec(json.dumps({"version": 2, "task_id": "x"})) is None


def test_to_event_reconstructs() -> None:
    from openbot.domain.events import EventKind
    event = _spec().to_event()
    assert event.repo == "org/repo"
    assert event.kind is EventKind.ISSUE_OPENED


def test_initial_labels_present() -> None:
    spec = _spec(initial_labels=["cancel-openbot", "bug"])
    assert "cancel-openbot" in spec.initial_labels
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

```bash
cd /Users/wy/projects/openbot && uv run pytest tests/infrastructure/queue/test_task_spec.py -v 2>&1 | head -10
```

- [ ] **Step 3: Implement task_spec.py**

```python
# openbot/infrastructure/queue/task_spec.py
"""TaskSpec v3 — self-contained worker input contract.

Discriminated from QueuePayload (which uses "version": 1|2) by "spec_version": 3.
Built by the webhook async segment after running full preflight. The worker
trusts this and executes the handler without re-running any preflight gates.
"""
from __future__ import annotations
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from openbot.domain.events import EventKind, UnifiedEvent

if TYPE_CHECKING:
    from openbot.application.router import Dispatch

_logger = logging.getLogger(__name__)
TASK_SPEC_VERSION: Final = 3


@dataclass(frozen=True, slots=True)
class DecisionStep:
    """One middleware's outcome in the webhook preflight decision trace."""
    middleware: str
    outcome: str   # "proceed" | "blocked"
    reason: str | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """TaskSpec v3 — worker input contract built by webhook async segment."""
    spec_version: int          # always 3
    # identity
    task_id: str
    run_id: str
    prev_run_id: str | None
    resource_key: str | None
    event_seq: int
    intent: str                # "start" | "supersede" | "cancel"
    enqueued_at: str           # ISO-8601 UTC
    spec_built_at: str         # ISO-8601 UTC (when webhook built this)
    # scenario
    scenario: str              # Feature.value
    # event reconstruction
    channel: str
    delivery_id: str
    kind: str                  # EventKind.value
    repo: str
    actor: str
    actor_type: str | None
    issue_number: int | None
    pr_number: int | None
    comment_body: str | None
    installation_id: int | None
    raw: dict[str, Any]
    check_run_id: int | None
    # decision proof
    decision_trace: list[dict[str, Any]]
    classifier_skipped: bool   # True in F1
    stages_to_run: list[str]   # [] = all stages (F1 default)
    # cancel quick-check
    initial_labels: list[str]  # label snapshot at spec-build time

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    def to_event(self) -> UnifiedEvent:
        try:
            kind = EventKind(self.kind)
        except ValueError:
            _logger.warning("task_spec_unknown_kind",
                            extra={"delivery_id": self.delivery_id, "kind_raw": self.kind})
            kind = EventKind.UNKNOWN
        return UnifiedEvent(
            channel=self.channel, delivery_id=self.delivery_id, kind=kind,
            repo=self.repo, actor=self.actor, actor_type=self.actor_type,
            issue_number=self.issue_number, pr_number=self.pr_number,
            comment_body=self.comment_body, installation_id=self.installation_id,
            event_seq=self.event_seq, raw=self.raw,
        )

    @classmethod
    def from_event_and_dispatch(
        cls,
        event: UnifiedEvent,
        dispatch: Dispatch,
        *,
        check_run_id: int | None = None,
        decision_trace: list[dict[str, Any]] | None = None,
        initial_labels: list[str] | None = None,
    ) -> TaskSpec:
        now = datetime.now(UTC).isoformat()
        return cls(
            spec_version=TASK_SPEC_VERSION,
            task_id=dispatch.task_id,
            run_id=dispatch.run_id or dispatch.task_id,
            prev_run_id=dispatch.prev_run_id,
            resource_key=dispatch.resource_key,
            event_seq=dispatch.event_seq,
            intent=dispatch.intent or "start",
            enqueued_at=now, spec_built_at=now,
            scenario=dispatch.feature.value,
            channel=event.channel, delivery_id=event.delivery_id,
            kind=event.kind.value, repo=event.repo, actor=event.actor,
            actor_type=event.actor_type, issue_number=event.issue_number,
            pr_number=event.pr_number, comment_body=event.comment_body,
            installation_id=event.installation_id, raw=event.raw,
            check_run_id=check_run_id,
            decision_trace=decision_trace or [],
            classifier_skipped=True,
            stages_to_run=[],
            initial_labels=initial_labels or [],
        )


def deserialize_task_spec(blob: str | bytes) -> TaskSpec | None:
    """Parse v3 TaskSpec blob. Returns None on bad JSON or wrong spec_version."""
    try:
        text = blob.decode("utf-8") if isinstance(blob, bytes | bytearray) else blob
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _logger.warning("task_spec_parse_failed",
                        extra={"reason": f"{type(exc).__name__}: {exc}"})
        return None
    if not isinstance(data, dict) or data.get("spec_version") != TASK_SPEC_VERSION:
        return None
    try:
        return TaskSpec(**data)
    except TypeError as exc:
        _logger.warning("task_spec_schema_drift",
                        extra={"reason": str(exc)[:200],
                               "delivery_id": data.get("delivery_id")})
        return None
```

- [ ] **Step 4: Run tests — expect 7 PASS**

```bash
cd /Users/wy/projects/openbot && uv run pytest tests/infrastructure/queue/test_task_spec.py -v
```

- [ ] **Step 5: Commit**

```bash
cd /Users/wy/projects/openbot && git add openbot/infrastructure/queue/task_spec.py tests/infrastructure/queue/test_task_spec.py
git commit -m "feat(queue): add TaskSpec v3 schema and deserializer"
```

---

## Task 2: Extend QueuePort + enqueue infrastructure

**Files:**
- Modify: `openbot/application/ports/queue.py`
- Modify: `openbot/infrastructure/queue/enqueue.py`
- Modify: `tests/_fakes/queue.py`

- [ ] **Step 1: Update QueuePort protocol**

```python
# openbot/application/ports/queue.py — replace entire file
"""QueuePort — enqueue events and task specs for the worker."""
from __future__ import annotations
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from openbot.domain.events import UnifiedEvent
    from openbot.domain.workflows import Feature
    from openbot.infrastructure.queue.task_spec import TaskSpec


@runtime_checkable
class QueuePort(Protocol):
    """Enqueue one event onto the Redis Stream."""

    async def enqueue(
        self,
        event: UnifiedEvent,
        *,
        feature: Feature,
        task_id: str,
        check_run_id: int | None = None,
        intent: str | None = None,
        run_id: str | None = None,
        prev_run_id: str | None = None,
        resource_key: str | None = None,
        event_seq: int = 0,
    ) -> str:
        """Returns the Redis stream entry ID (v1/v2 QueuePayload path)."""
        ...

    async def enqueue_task_spec(self, spec: TaskSpec) -> str:
        """Enqueue a pre-built TaskSpec v3.

        Returns the Redis stream entry ID. Used by decide_and_enqueue()
        to push a fully-decided TaskSpec to the worker queue.
        """
        ...
```

- [ ] **Step 2: Update FakeQueue**

```python
# tests/_fakes/queue.py — replace entire file
"""FakeQueue — in-memory QueuePort."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openbot.domain.events import UnifiedEvent
    from openbot.domain.workflows import Feature
    from openbot.infrastructure.queue.task_spec import TaskSpec


@dataclass
class FakeQueue:
    calls: list[dict[str, Any]] = field(default_factory=list)
    task_specs: list[Any] = field(default_factory=list)  # list[TaskSpec]
    next_id: int = 0

    async def enqueue(
        self,
        event: UnifiedEvent,
        *,
        feature: Feature,
        task_id: str,
        check_run_id: int | None = None,
        intent: str | None = None,
        run_id: str | None = None,
        prev_run_id: str | None = None,
        resource_key: str | None = None,
        event_seq: int = 0,
    ) -> str:
        self.calls.append(dict(
            event=event, feature=feature, task_id=task_id,
            check_run_id=check_run_id, intent=intent, run_id=run_id,
            prev_run_id=prev_run_id, resource_key=resource_key,
            event_seq=event_seq,
        ))
        sid = f"0-{self.next_id}"
        self.next_id += 1
        return sid

    async def enqueue_task_spec(self, spec: TaskSpec) -> str:
        self.task_specs.append(spec)
        sid = f"0-{self.next_id}"
        self.next_id += 1
        return sid
```

- [ ] **Step 3: Add enqueue_task_spec() to enqueue.py**

Open `openbot/infrastructure/queue/enqueue.py`. After the existing `enqueue()` function, add:

```python
# Add this import at the top of enqueue.py
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from openbot.infrastructure.queue.task_spec import TaskSpec


async def enqueue_task_spec(redis: redis_async.Redis, spec: "TaskSpec") -> str:
    """XADD a TaskSpec v3 JSON blob to the work stream.

    Uses the same {"json": <blob>} field layout as QueuePayload.
    The worker discriminates by peeking at spec_version before deserializing.
    """
    entry_id = await redis.xadd(
        STREAM_NAME,
        {"json": spec.to_json()},
        maxlen=MAX_STREAM_LEN,
        approximate=True,
    )
    if isinstance(entry_id, bytes | bytearray):
        entry_id = entry_id.decode("ascii", errors="replace")
    _logger.info(
        "task_spec_enqueued",
        extra={
            "delivery_id": spec.delivery_id,
            "repo": spec.repo,
            "scenario": spec.scenario,
            "task_id": spec.task_id,
            "entry_id": entry_id,
        },
    )
    return entry_id
```

Also add `enqueue_task_spec` method to `RedisStreamQueue` class:

```python
# Inside class RedisStreamQueue in enqueue.py:
    async def enqueue_task_spec(self, spec: object) -> str:
        from openbot.infrastructure.queue.task_spec import TaskSpec as _T
        assert isinstance(spec, _T)
        return await enqueue_task_spec(self._redis, spec)
```

- [ ] **Step 4: Run queue port contract tests**

```bash
cd /Users/wy/projects/openbot && uv run pytest tests/application/ports/test_queue_port_contract.py -v
```
Expected: All PASS (the `isinstance(FakeQueue(), QueuePort)` check works if Protocol is `runtime_checkable`).

**Note:** If Protocol structural check fails for `enqueue_task_spec`, confirm `@runtime_checkable` is on QueuePort and FakeQueue implements both methods.

- [ ] **Step 5: Run full suite — no regressions**

```bash
cd /Users/wy/projects/openbot && make check 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
cd /Users/wy/projects/openbot && git add openbot/application/ports/queue.py openbot/infrastructure/queue/enqueue.py tests/_fakes/queue.py
git commit -m "feat(queue): add enqueue_task_spec() to QueuePort, RedisStreamQueue, FakeQueue"
```

---

## Task 3: execute_handler() in dispatcher.py

**Files:**
- Modify: `openbot/application/dispatcher.py`
- Create: `tests/application/dispatcher/__init__.py`
- Create: `tests/application/dispatcher/test_execute_handler.py`

- [ ] **Step 1: Create test package**

```bash
mkdir -p /Users/wy/projects/openbot/tests/application/dispatcher
touch /Users/wy/projects/openbot/tests/application/dispatcher/__init__.py
```

- [ ] **Step 2: Write failing test**

```python
# tests/application/dispatcher/test_execute_handler.py
"""execute_handler — handler invocation without preflight."""
from __future__ import annotations
import pytest
from openbot.application.dispatcher import execute_handler
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests.conftest import make_issue_opened_event


@pytest.mark.asyncio
async def test_execute_handler_calls_handler(monkeypatch) -> None:
    event = make_issue_opened_event()
    from openbot.application.router import dispatch_for
    dispatch = dispatch_for(event)
    assert dispatch is not None

    config = await FakeConfigLoader().load_for_repo(None, event)
    called = []

    async def fake_handler(ctx) -> None:
        called.append(ctx.event.delivery_id)

    from dataclasses import replace
    dispatch = replace(dispatch, handler=fake_handler)

    await execute_handler(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config=config,
        session_factory=None,
        redis=None,
    )
    assert called == [event.delivery_id]


@pytest.mark.asyncio
async def test_execute_handler_never_raises() -> None:
    event = make_issue_opened_event()
    from openbot.application.router import dispatch_for
    dispatch = dispatch_for(event)
    assert dispatch is not None
    config = await FakeConfigLoader().load_for_repo(None, event)

    async def crashing(ctx) -> None:
        raise RuntimeError("boom")

    from dataclasses import replace
    dispatch = replace(dispatch, handler=crashing)

    # Must not raise
    await execute_handler(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config=config,
        session_factory=None,
        redis=None,
    )
```

- [ ] **Step 3: Run — expect ImportError**

```bash
cd /Users/wy/projects/openbot && uv run pytest tests/application/dispatcher/test_execute_handler.py -v 2>&1 | head -10
```

- [ ] **Step 4: Add execute_handler() to dispatcher.py**

Add this function after the existing `run_dispatch()` in `openbot/application/dispatcher.py`:

```python
async def execute_handler(
    *,
    adapter: ChannelAdapterPort,
    event: UnifiedEvent,
    dispatch: Dispatch,
    config: EffectiveConfig,
    session_factory: async_sessionmaker[AsyncSession] | None,
    redis: redis_async.Redis | None,
    check_run_id: int | None = None,
    audit: AuditLogPort | None = None,
    rate_limiter: RateLimiterPort | None = None,
) -> None:
    """Execute workflow handler directly — no preflight.

    Used by the worker when processing a TaskSpec v3: the webhook async
    segment already ran the full preflight chain. Never raises out.
    """
    ctx = PreflightContext(
        event=event, dispatch=dispatch, config=config, adapter=adapter,
        session_factory=session_factory, redis=redis,
        check_run_id=check_run_id, audit=audit, rate_limiter=rate_limiter,
    )
    try:
        await dispatch.handler(ctx)
        if check_run_id:
            try:
                await adapter.update_check_run(
                    event, check_run_id,
                    status="completed", conclusion="success",
                    output={"title": "Analysis Complete",
                            "summary": f"Workflow `{dispatch.feature.value}` finished."},
                )
            except Exception:
                _logger.exception("check_run_update_failed_on_success")
    except Exception:
        _logger.exception(
            "workflow_handler_crashed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo,
                   "feature": dispatch.feature.value},
        )
        if check_run_id:
            try:
                await adapter.update_check_run(
                    event, check_run_id,
                    status="completed", conclusion="failure",
                    output={"title": "Handler Crash",
                            "summary": f"Handler `{dispatch.feature.value}` raised unexpectedly."},
                )
            except Exception:
                _logger.exception("check_run_update_failed_on_handler_crash")
```

Also update `__all__` at the bottom of `dispatcher.py`:

```python
__all__ = ["build_preflight_chain", "execute_handler", "run_dispatch"]
```

Add `EffectiveConfig` and `Dispatch` to the `TYPE_CHECKING` block if not already present:

```python
if TYPE_CHECKING:
    # (existing imports)
    from openbot.domain.config_schema import EffectiveConfig
    from openbot.application.router import Dispatch
```

- [ ] **Step 5: Run — expect 2 PASS**

```bash
cd /Users/wy/projects/openbot && uv run pytest tests/application/dispatcher/test_execute_handler.py -v
```

- [ ] **Step 6: Run full suite**

```bash
cd /Users/wy/projects/openbot && make check 2>&1 | tail -5
```

- [ ] **Step 7: Commit**

```bash
cd /Users/wy/projects/openbot && git add openbot/application/dispatcher.py tests/application/dispatcher/__init__.py tests/application/dispatcher/test_execute_handler.py
git commit -m "feat(dispatcher): add execute_handler() for TaskSpec v3 worker path"
```

---

> **Continue to Part 2:** `2026-05-20-webhook-worker-layering-f1-part2.md`
> Tasks 4–8: decide_and_enqueue(), BackgroundTask wiring, worker v3 path, F-series tests, final verification.
