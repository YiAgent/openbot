# Phase 2b — State-Machine Ports (Tasks 2.4 – 2.7)

> Continues from [`phase-2a`](2026-05-18-hexagonal-restructure-phase-2a-ports-core.md). Continues into [`phase-2c`](2026-05-18-hexagonal-restructure-phase-2c-ports-rest.md).

**Goal of Phase 2b:** introduce the four Ports covering the state-machine surface — `RunsRepoPort` (CAS transition), `ResourceLockPort` (per-resource mutex), `CancellationPort` (cross-dyno signal), `AuditLogPort` (durable trail). After Task 2.7 the `application/state/* -> infrastructure/persistence/*` ignore line is **removed** from `.importlinter`.

4 tasks, 4 commits. Each task follows the same shape as Phase 2a (Protocol → witness → consumer switch → Fake → contract test).

---

## Task 2.4: `RunsRepoPort`

**Spec mapping:** `application.state.runs_repo.transition` is a free function over an `AsyncSession`. We wrap it in a class so consumers depend on a Port instance and never touch SQLAlchemy directly.

**Files:**
- Create: `openbot/application/ports/runs_repo.py`
- Create: `openbot/infrastructure/persistence/runs_repo_impl.py`
- Modify: `openbot/entrypoints/api/app.py` — attach `app.state.runs_repo`
- Modify: `openbot/entrypoints/api/routes/github_webhook.py` — call via Port
- Modify: `openbot/application/ports/__init__.py` — re-export
- Modify: `.importlinter` — remove `openbot.application.state.* -> openbot.infrastructure.persistence.*`
- Create: `tests/_fakes/runs_repo.py`
- Create: `tests/application/ports/test_runs_repo_port_contract.py`

- [ ] **Step 1: Write `application/ports/runs_repo.py`**

```python
"""RunsRepoPort — state-machine CAS write surface."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from openbot.application.state.runs_repo import TransitionResult
    from openbot.domain.events import UnifiedEvent


class RunsRepoPort(Protocol):
    """Persisted state machine keyed by `resource_key`."""

    async def transition(
        self, *, event: "UnifiedEvent", new_run_id: str
    ) -> "TransitionResult":
        """Classify + CAS-write. See state.runs_repo.transition for semantics."""
        ...
```

`TransitionResult` is an application-layer dataclass — Port importing it from `application.state` is in-layer.

Append to `openbot/application/ports/__init__.py`:

```python
from openbot.application.ports.runs_repo import RunsRepoPort  # noqa: F401
```

- [ ] **Step 2: Write `infrastructure/persistence/runs_repo_impl.py`**

```python
"""SQLAlchemy-backed RunsRepoPort implementation.

Wraps the free `application.state.runs_repo.transition` function: opens a
session per call, commits on success, lets exceptions propagate so the
caller decides what to do.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from openbot.application.state.runs_repo import TransitionResult, transition

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from openbot.application.ports.runs_repo import RunsRepoPort
    from openbot.domain.events import UnifiedEvent


class SqlRunsRepo:
    def __init__(
        self, session_factory: "Callable[[], AsyncSession]"
    ) -> None:
        self._session_factory = session_factory

    async def transition(
        self, *, event: "UnifiedEvent", new_run_id: str
    ) -> TransitionResult:
        async with self._session_factory() as session:
            result = await transition(session, event=event, new_run_id=new_run_id)
            await session.commit()
            return result


if TYPE_CHECKING:
    _witness: "RunsRepoPort" = SqlRunsRepo(session_factory=lambda: None)  # type: ignore[arg-type, return-value]
```

The `TYPE_CHECKING` witness import is `application/ports/runs_repo.py`, which itself imports `application/state/runs_repo.py` — both are in-layer so no contract violation.

- [ ] **Step 3: Switch the consumer**

In `openbot/entrypoints/api/app.py` lifespan:

```python
from openbot.infrastructure.persistence.runs_repo_impl import SqlRunsRepo
# ...
app.state.runs_repo = SqlRunsRepo(session_factory)
```

In `openbot/entrypoints/api/routes/github_webhook.py`, replace the inline session+transition block:

```python
# was:
async with request.app.state.session_factory() as session:
    tr = await transition(session, event=event, new_run_id=run_id)
    await session.commit()

# becomes:
tr = await request.app.state.runs_repo.transition(event=event, new_run_id=run_id)
```

Remove the now-unused `from openbot.application.state.runs_repo import transition` import. The `TransitionResult` import stays if it's referenced for typing.

- [ ] **Step 4: Delete the ignore line**

Edit `.importlinter`:

```diff
  ignore_imports =
      openbot.application.middleware.* -> openbot.infrastructure.persistence.*
-     openbot.application.state.* -> openbot.infrastructure.persistence.*
      openbot.application.middleware.* -> openbot.infrastructure.llm.*
      ...
```

Run `uv run lint-imports` standalone to confirm: if any `application.state.*` still imports `infrastructure.persistence.*`, the linter will surface it. The only legitimate remaining edge case is `application.state.cancellation.signal` reading `redis_async.Redis` as a parameter — that's a third-party arrow, not an `openbot.*` arrow, so it's fine.

- [ ] **Step 5: Write `tests/_fakes/runs_repo.py`**

```python
"""FakeRunsRepo — programmable TransitionResult queue."""
from __future__ import annotations

from dataclasses import dataclass, field

from openbot.application.state.runs_repo import TransitionResult
from openbot.domain.events import UnifiedEvent


@dataclass
class FakeRunsRepo:
    queued: list[TransitionResult] = field(default_factory=list)
    calls: list[tuple[UnifiedEvent, str]] = field(default_factory=list)

    async def transition(
        self, *, event: UnifiedEvent, new_run_id: str
    ) -> TransitionResult:
        self.calls.append((event, new_run_id))
        if not self.queued:
            raise AssertionError("FakeRunsRepo: no result queued for transition()")
        return self.queued.pop(0)
```

- [ ] **Step 6: Write the contract test**

Create `tests/application/ports/test_runs_repo_port_contract.py`:

```python
"""Contract test — FakeRunsRepo conforms to RunsRepoPort structurally."""
from __future__ import annotations

import pytest

from openbot.application.state.runs_repo import TransitionResult
from openbot.domain.events import EventKind, UnifiedEvent
from tests._fakes.runs_repo import FakeRunsRepo


def test_fake_has_transition_method() -> None:
    # RunsRepoPort is not @runtime_checkable (it carries kw-only params).
    # Structural check via attribute access is enough.
    assert callable(getattr(FakeRunsRepo(), "transition", None))


@pytest.mark.asyncio
async def test_queued_result_returned_in_order() -> None:
    from openbot.application.state.classifier import EventClassification
    from openbot.application.state.runs_repo import State
    from openbot.domain.intents import Intent  # adjust to domain.intents path

    repo = FakeRunsRepo()
    repo.queued = [
        TransitionResult(
            classification=EventClassification(
                intent=Intent.NEW, next_state=State.RUNNING, reason="ok"
            ),
            run_id="r1",
            prev_run_id=None,
            prior_state=State.IDLE,
        )
    ]
    ev = UnifiedEvent(kind=EventKind.UNKNOWN, channel="github")
    out = await repo.transition(event=ev, new_run_id="r1")
    assert out.run_id == "r1"
    assert repo.calls == [(ev, "r1")]
```

If `Intent` already lives at `openbot.domain.intents` (after Phase 1a Task 1.2), use that path. If `UnifiedEvent` needs additional kwargs, pass them — DO NOT change the dataclass.

- [ ] **Step 7: Run tests**

```bash
make check
```
Expected: 547 passed. `lint-imports` green with **one fewer** ignore line.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(ports): introduce RunsRepoPort + SqlRunsRepo adapter"
```

---

## Task 2.5: `ResourceLockPort`

**Spec mapping:** `resource_lock(redis, resource_key, *, ttl_seconds)` is an `@asynccontextmanager`. The Port exposes `lock(...)` returning an async-CM.

**Files:**
- Create: `openbot/application/ports/resource_lock.py`
- Create: `openbot/infrastructure/persistence/resource_lock_redis.py`
- Modify: `openbot/entrypoints/api/app.py` — attach
- Modify: `openbot/entrypoints/api/routes/github_webhook.py` — call via Port
- Modify: `openbot/application/ports/__init__.py` — re-export
- Create: `tests/_fakes/resource_lock.py`
- Create: `tests/application/ports/test_resource_lock_port_contract.py`

- [ ] **Step 1: Write `application/ports/resource_lock.py`**

```python
"""ResourceLockPort — per-resource_key mutual exclusion."""
from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol


class ResourceLockPort(Protocol):
    """Acquire and hold a per-resource lock for the duration of a `with`."""

    def lock(
        self, resource_key: str, *, ttl_seconds: int = 30
    ) -> AbstractAsyncContextManager[bool]:
        """Async-CM yielding True if acquired (or fallback-open), False on contention."""
        ...
```

Append to `openbot/application/ports/__init__.py`:

```python
from openbot.application.ports.resource_lock import ResourceLockPort  # noqa: F401
```

- [ ] **Step 2: Write `infrastructure/persistence/resource_lock_redis.py`**

```python
"""Redis-backed ResourceLockPort."""
from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

import redis.asyncio as redis_async

from openbot.application.state.resource_lock import resource_lock

if TYPE_CHECKING:
    from openbot.application.ports.resource_lock import ResourceLockPort


class RedisResourceLock:
    def __init__(self, redis: redis_async.Redis | None) -> None:
        self._redis = redis

    def lock(
        self, resource_key: str, *, ttl_seconds: int = 30
    ) -> AbstractAsyncContextManager[bool]:
        return resource_lock(self._redis, resource_key, ttl_seconds=ttl_seconds)


if TYPE_CHECKING:
    _witness: "ResourceLockPort" = RedisResourceLock(redis=None)
```

- [ ] **Step 3: Switch the consumer**

In `openbot/entrypoints/api/app.py` lifespan:

```python
from openbot.infrastructure.persistence.resource_lock_redis import RedisResourceLock
# ...
app.state.resource_lock = RedisResourceLock(redis)
```

In the route (or wherever `resource_lock` is currently called), replace:

```python
from openbot.application.state.resource_lock import resource_lock
# ...
async with resource_lock(request.app.state.redis, resource_key) as acquired:
    ...
```

with:

```python
async with request.app.state.resource_lock.lock(resource_key) as acquired:
    ...
```

- [ ] **Step 4: Write `tests/_fakes/resource_lock.py`**

```python
"""FakeResourceLock — no-op CM that records acquisitions."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class FakeResourceLock:
    calls: list[tuple[str, int]] = field(default_factory=list)
    acquire_result: bool = True

    @asynccontextmanager
    async def lock(self, resource_key: str, *, ttl_seconds: int = 30):
        self.calls.append((resource_key, ttl_seconds))
        yield self.acquire_result
```

The decorator-on-method pattern returns an async-CM at call time, satisfying the structural Protocol.

- [ ] **Step 5: Write the contract test**

Create `tests/application/ports/test_resource_lock_port_contract.py`:

```python
"""Contract test — FakeResourceLock behaves like an async CM."""
from __future__ import annotations

import pytest

from tests._fakes.resource_lock import FakeResourceLock


@pytest.mark.asyncio
async def test_lock_records_acquisition() -> None:
    lock = FakeResourceLock()
    async with lock.lock("github:owner/repo:pr:1") as acquired:
        assert acquired is True
    assert lock.calls == [("github:owner/repo:pr:1", 30)]


@pytest.mark.asyncio
async def test_lock_contention_yields_false() -> None:
    lock = FakeResourceLock(acquire_result=False)
    async with lock.lock("github:owner/repo:pr:2") as acquired:
        assert acquired is False
```

- [ ] **Step 6: Run tests**

```bash
make check
```
Expected: 548 passed. `lint-imports` green.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(ports): introduce ResourceLockPort + RedisResourceLock adapter"
```

---

## Task 2.6: `CancellationPort`

**Spec mapping:** `application.state.cancellation` exposes `signal`, `is_cancelled`, `checkpoint`, `register`, `deregister`. The Port surfaces ONLY `signal` and `is_cancelled` — those are cross-dyno I/O. `register`/`deregister`/`checkpoint` are per-task locals that don't need to be pluggable.

**Files:**
- Create: `openbot/application/ports/cancellation.py`
- Create: `openbot/infrastructure/persistence/cancellation_redis.py`
- Modify: `openbot/entrypoints/api/app.py` — attach
- Modify: `openbot/entrypoints/api/routes/github_webhook.py` — call via Port for `signal`
- Modify: `openbot/application/ports/__init__.py` — re-export
- Create: `tests/_fakes/cancellation.py`
- Create: `tests/application/ports/test_cancellation_port_contract.py`

- [ ] **Step 1: Write `application/ports/cancellation.py`**

```python
"""CancellationPort — cross-dyno run cancellation signal."""
from __future__ import annotations

from typing import Protocol


class CancellationPort(Protocol):
    """Signal/check a cancellation flag durable across dynos."""

    async def signal(self, run_id: str) -> None:
        """Mark run_id as cancelled. Sets the Redis flag AND cancels local task."""
        ...

    async def is_cancelled(self, run_id: str) -> bool:
        """Cheap point query — returns False on Redis flap (fail-closed)."""
        ...
```

Append to `openbot/application/ports/__init__.py`:

```python
from openbot.application.ports.cancellation import CancellationPort  # noqa: F401
```

- [ ] **Step 2: Write `infrastructure/persistence/cancellation_redis.py`**

```python
"""Redis-backed CancellationPort."""
from __future__ import annotations

from typing import TYPE_CHECKING

import redis.asyncio as redis_async

from openbot.application.state.cancellation import is_cancelled, signal

if TYPE_CHECKING:
    from openbot.application.ports.cancellation import CancellationPort


class RedisCancellation:
    def __init__(self, redis: redis_async.Redis | None) -> None:
        self._redis = redis

    async def signal(self, run_id: str) -> None:
        await signal(self._redis, run_id)

    async def is_cancelled(self, run_id: str) -> bool:
        return await is_cancelled(self._redis, run_id)


if TYPE_CHECKING:
    _witness: "CancellationPort" = RedisCancellation(redis=None)
```

- [ ] **Step 3: Switch the consumer**

In `openbot/entrypoints/api/routes/github_webhook.py`, replace:

```python
from openbot.application.state.cancellation import signal as cancellation_signal
# ...
await cancellation_signal(request.app.state.redis, prev_run_id)
```

with:

```python
# (no import needed — go through state)
await request.app.state.cancellation.signal(prev_run_id)
```

In `openbot/entrypoints/api/app.py` lifespan:

```python
from openbot.infrastructure.persistence.cancellation_redis import RedisCancellation
# ...
app.state.cancellation = RedisCancellation(redis)
```

Worker-side callers (`infrastructure/queue/worker.py`) still call `signal`/`is_cancelled` directly — they will be rewired once `WorkerDeps` lands. Mark each call site with a TODO:

```python
# TODO(phase-2c): route through CancellationPort once worker composition root lands.
```

- [ ] **Step 4: Write `tests/_fakes/cancellation.py`**

```python
"""FakeCancellation — in-memory CancellationPort."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeCancellation:
    cancelled: set[str] = field(default_factory=set)
    signal_calls: list[str] = field(default_factory=list)

    async def signal(self, run_id: str) -> None:
        self.signal_calls.append(run_id)
        self.cancelled.add(run_id)

    async def is_cancelled(self, run_id: str) -> bool:
        return run_id in self.cancelled
```

- [ ] **Step 5: Write the contract test**

Create `tests/application/ports/test_cancellation_port_contract.py`:

```python
"""Contract test — FakeCancellation respects CancellationPort semantics."""
from __future__ import annotations

import pytest

from tests._fakes.cancellation import FakeCancellation


@pytest.mark.asyncio
async def test_signal_then_is_cancelled() -> None:
    c = FakeCancellation()
    assert await c.is_cancelled("r1") is False
    await c.signal("r1")
    assert await c.is_cancelled("r1") is True
    assert c.signal_calls == ["r1"]
```

- [ ] **Step 6: Run tests**

```bash
make check
```
Expected: 549 passed. `lint-imports` green.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(ports): introduce CancellationPort + RedisCancellation adapter"
```

---

## Task 2.7: `AuditLogPort`

**Spec mapping:** today's audit-log writes happen inline in middleware (one of the `application.middleware.* -> infrastructure.persistence.*` leaks). The Port pulls them out.

**Files:**
- Create: `openbot/application/ports/audit_log.py`
- Create: `openbot/infrastructure/persistence/audit_log_repo.py`
- Modify: `openbot/application/middleware/audit.py` — accept an `AuditLogPort` and write through it
- Modify: `openbot/entrypoints/api/app.py` — attach
- Modify: `openbot/application/ports/__init__.py` — re-export
- Modify: `.importlinter` — remove `openbot.application.middleware.* -> openbot.infrastructure.persistence.*`
- Create: `tests/_fakes/audit_log.py`
- Create: `tests/application/ports/test_audit_log_port_contract.py`

- [ ] **Step 1: Inspect today's audit write surface**

```bash
grep -n "audit" openbot/application/middleware/audit.py | head -40
```

Identify the function/class that today writes a row directly via `AsyncSession` or `AuditLogRepo`. The Port signature mirrors that one call.

- [ ] **Step 2: Write `application/ports/audit_log.py`**

```python
"""AuditLogPort — durable trail of dispatched events."""
from __future__ import annotations

from typing import Any, Protocol


class AuditLogPort(Protocol):
    """Append-only row writer.

    The schema is fixed in `infrastructure/persistence/models.AuditLog`;
    callers pass channel-agnostic kwargs and the impl maps them.
    """

    async def record(
        self,
        *,
        run_id: str,
        resource_key: str | None,
        kind: str,
        intent: str,
        reason: str | None,
        payload: dict[str, Any] | None = None,
    ) -> None: ...
```

(Adjust the kwarg list to match whatever the today-inline writer takes. The shape above is a reasonable starting set.)

Append to `openbot/application/ports/__init__.py`:

```python
from openbot.application.ports.audit_log import AuditLogPort  # noqa: F401
```

- [ ] **Step 3: Write `infrastructure/persistence/audit_log_repo.py`**

```python
"""SQL-backed AuditLogPort."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openbot.infrastructure.persistence.models import AuditLog

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from openbot.application.ports.audit_log import AuditLogPort


class AuditLogRepo:
    def __init__(
        self, session_factory: "Callable[[], AsyncSession]"
    ) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        run_id: str,
        resource_key: str | None,
        kind: str,
        intent: str,
        reason: str | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                AuditLog(
                    run_id=run_id,
                    resource_key=resource_key,
                    kind=kind,
                    intent=intent,
                    reason=reason,
                    payload=payload or {},
                )
            )
            await session.commit()


if TYPE_CHECKING:
    _witness: "AuditLogPort" = AuditLogRepo(session_factory=lambda: None)  # type: ignore[arg-type, return-value]
```

If the `AuditLog` model has different column names, mirror them faithfully. Do NOT rename schema columns.

- [ ] **Step 4: Switch the middleware**

Edit `openbot/application/middleware/audit.py`:
- Replace the inline session+row-write with a call to `deps.audit.record(...)` where `deps` is the `DispatcherDeps` passed in at construction time.
- The middleware constructor signature changes from `AuditMiddleware(session_factory=...)` to `AuditMiddleware(audit: AuditLogPort)`.
- Remove the `from openbot.infrastructure.persistence...` imports from `audit.py`.

Update every constructor call site (search for `AuditMiddleware(`) to pass the Port instance instead of `session_factory`. The composition root in `entrypoints/api/app.py` is where the Port is built:

```python
from openbot.infrastructure.persistence.audit_log_repo import AuditLogRepo
# ...
app.state.audit = AuditLogRepo(session_factory)
```

- [ ] **Step 5: Delete the ignore line**

Edit `.importlinter`:

```diff
- openbot.application.middleware.* -> openbot.infrastructure.persistence.*
```

If `audit.py` was the LAST middleware that imported `infrastructure.persistence.*`, the line goes away cleanly. If others remain (e.g. budget, rate_limit), they belong to later tasks — keep the ignore narrower:

```diff
- openbot.application.middleware.* -> openbot.infrastructure.persistence.*
+ openbot.application.middleware.budget -> openbot.infrastructure.persistence.*
+ openbot.application.middleware.rate_limit -> openbot.infrastructure.persistence.*
```

Run `uv run lint-imports` and let the output dictate which narrower lines stay.

- [ ] **Step 6: Write `tests/_fakes/audit_log.py`**

```python
"""FakeAuditLog — in-memory AuditLogPort."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeAuditLog:
    rows: list[dict[str, Any]] = field(default_factory=list)

    async def record(
        self,
        *,
        run_id: str,
        resource_key: str | None,
        kind: str,
        intent: str,
        reason: str | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.rows.append(
            {
                "run_id": run_id,
                "resource_key": resource_key,
                "kind": kind,
                "intent": intent,
                "reason": reason,
                "payload": payload or {},
            }
        )
```

- [ ] **Step 7: Write the contract test**

Create `tests/application/ports/test_audit_log_port_contract.py`:

```python
"""Contract test — FakeAuditLog records every field."""
from __future__ import annotations

import pytest

from tests._fakes.audit_log import FakeAuditLog


@pytest.mark.asyncio
async def test_record_persists_kwargs() -> None:
    a = FakeAuditLog()
    await a.record(
        run_id="r1",
        resource_key="github:owner/repo:pr:1",
        kind="event",
        intent="NEW",
        reason="ok",
        payload={"foo": "bar"},
    )
    assert a.rows == [
        {
            "run_id": "r1",
            "resource_key": "github:owner/repo:pr:1",
            "kind": "event",
            "intent": "NEW",
            "reason": "ok",
            "payload": {"foo": "bar"},
        }
    ]
```

- [ ] **Step 8: Run tests**

```bash
make check
```
Expected: 550 passed. `lint-imports` green.

If existing tests in `tests/application/middleware/test_audit.py` (or similar) break because they passed `session_factory=...` to the constructor, update them to pass `audit=FakeAuditLog()` instead. This is a single-file mechanical pass.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(ports): introduce AuditLogPort + AuditLogRepo adapter"
```

---

## End of Phase 2b

At this point:
- `application/ports/` holds 7 Protocol files.
- `tests/_fakes/` holds 7 fakes, all contract-tested.
- `app.state` carries Port-typed `runs_repo`, `resource_lock`, `cancellation`, `audit` collaborators.
- `entrypoints/api/routes/github_webhook.py` no longer imports `openbot.application.state.*` or `openbot.infrastructure.persistence.*` for these flows — only through Ports.
- `make check` reports **550 passed** (543 baseline + 7 contract tests).
- `.importlinter` has shed at least the `application.state.* -> infrastructure.persistence.*` line and probably the broad `application.middleware.* -> infrastructure.persistence.*` line.

Continue with [Phase 2c](2026-05-18-hexagonal-restructure-phase-2c-ports-rest.md): `RateLimiterPort`, `ConfigLoaderPort`, `LLMPort`, `SandboxPort`, and the empty-ignore-list checkpoint.
