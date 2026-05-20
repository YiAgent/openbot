# Phase 2a — Ports Scaffold + Dedup / Queue / Channel (Tasks 2.0 – 2.3)

> Part of [`2026-05-18-hexagonal-restructure.md`](2026-05-18-hexagonal-restructure.md). Continues into [`phase-2b`](2026-05-18-hexagonal-restructure-phase-2b-state-ports.md).

**Goal of Phase 2a:** scaffold `application/ports/`, introduce `DispatcherDeps`, build `tests/_fakes/`, then introduce the three Ports for the highest-traffic infrastructure surface (`DedupPort`, `QueuePort`, `ChannelAdapterPort`). Each Port task: define Protocol → make the infra impl satisfy it (via TYPE_CHECKING) → switch one consumer to read the Port off `app.state` → add a Fake in `tests/_fakes/` → add a contract test.

4 tasks, 4 commits.

---

## Task 2.0: Ports scaffold + `DispatcherDeps` + `tests/_fakes/`

No behavior change — only new files plus a frozen `DispatcherDeps` dataclass. Every Phase 2 task uses this scaffold.

**Files:**
- Modify: `openbot/application/ports/__init__.py` (created empty in Phase 1a Task 1.1)
- Create: `openbot/application/dispatcher_deps.py`
- Create: `tests/_fakes/__init__.py`
- Create: `tests/application/ports/__init__.py`

- [ ] **Step 1: Write `application/ports/__init__.py`**

```python
"""Application-layer Port catalogue.

Each Port is a `typing.Protocol` defined in its own module. Infrastructure
adapters satisfy these structurally — they import the Protocol only under
`TYPE_CHECKING` so the runtime arrow stays infra → domain, never
infra → application.

Each subsequent Port task appends one re-export here.
"""
from __future__ import annotations
```

- [ ] **Step 2: Write `application/dispatcher_deps.py`**

```python
"""Frozen bundle of Ports the dispatcher chain may need.

Built once per process at the composition root (api `deps.py` or worker
`__main__.py`). Middleware constructors accept a `DispatcherDeps` and read
only the Ports they need.

Fields are typed as Port Protocols, never as concrete infra types.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbot.application.ports.audit_log import AuditLogPort
    from openbot.application.ports.cancellation import CancellationPort
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.application.ports.config_loader import ConfigLoaderPort
    from openbot.application.ports.dedup import DedupPort
    from openbot.application.ports.llm import LLMPort
    from openbot.application.ports.queue import QueuePort
    from openbot.application.ports.rate_limiter import RateLimiterPort
    from openbot.application.ports.resource_lock import ResourceLockPort
    from openbot.application.ports.runs_repo import RunsRepoPort
    from openbot.application.ports.sandbox import SandboxPort


@dataclass(frozen=True)
class DispatcherDeps:
    """Every Port the chain may need. Composition root builds this once."""

    dedup: "DedupPort"
    queue: "QueuePort"
    channel: "ChannelAdapterPort"
    runs_repo: "RunsRepoPort"
    resource_lock: "ResourceLockPort"
    cancellation: "CancellationPort"
    audit: "AuditLogPort"
    rate_limiter: "RateLimiterPort"
    config_loader: "ConfigLoaderPort"
    llm: "LLMPort"
    sandbox: "SandboxPort"
```

- [ ] **Step 3: Create `tests/_fakes/__init__.py`**

```python
"""Port-shaped fakes for tests.

Each fake satisfies the matching Port Protocol structurally. Fakes expose
recorded calls as `.calls` (or a similarly-named attribute) for assertions.
"""
from __future__ import annotations
```

- [ ] **Step 4: Create `tests/application/ports/__init__.py`**

```bash
mkdir -p tests/application/ports
touch tests/application/ports/__init__.py
```

- [ ] **Step 5: Run tests**

```bash
make check
```
Expected: 543 passed, `lint-imports` green.

- [ ] **Step 6: Commit**

```bash
git add openbot/application/ports/__init__.py \
        openbot/application/dispatcher_deps.py \
        tests/_fakes/__init__.py \
        tests/application/ports/__init__.py
git commit -m "feat(application): scaffold ports/ and DispatcherDeps"
```

---

## Task 2.1: `DedupPort`

**Spec mapping:** `WebhookDedup.check_and_mark` → `DedupPort.check_and_mark`. Returns the existing `DedupOutcome` enum unchanged (spec §5 allows infra-owned leaf types).

**Files:**
- Create: `openbot/application/ports/dedup.py`
- Modify: `openbot/infrastructure/persistence/dedup.py` — add TYPE_CHECKING witness
- Modify: `openbot/entrypoints/api/app.py` — annotate `dedup` as `DedupPort`
- Modify: `openbot/application/ports/__init__.py` — re-export
- Modify: `.importlinter` — add the one allowed reverse arrow (Port → leaf enum)
- Create: `tests/_fakes/dedup.py`
- Create: `tests/application/ports/test_dedup_port_contract.py`

- [ ] **Step 1: Write `application/ports/dedup.py`**

```python
"""DedupPort — atomic delivery dedup contract."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from openbot.infrastructure.persistence.dedup import DedupOutcome


@runtime_checkable
class DedupPort(Protocol):
    """Atomic check-and-mark for (channel, delivery_id) pairs."""

    async def check_and_mark(
        self, channel: str, delivery_id: str
    ) -> DedupOutcome:
        """Returns FRESH, DUPLICATE, or FALLBACK_OPEN."""
        ...
```

**Layer-rule note:** `application/ports/dedup.py` imports `DedupOutcome` from infra. That's the one allowed reverse arrow (spec §5 — infra-owned leaf enum). Add to `.importlinter`:

```diff
  ignore_imports =
      ...
+     openbot.application.ports.dedup -> openbot.infrastructure.persistence.dedup
```

This entry persists past Phase 2b's "empty ignore list" because `DedupOutcome` is intentionally infra-owned.

- [ ] **Step 2: Append re-export**

Edit `openbot/application/ports/__init__.py`, append:

```python
from openbot.application.ports.dedup import DedupPort  # noqa: F401
```

- [ ] **Step 3: Add `TYPE_CHECKING` witness in infra**

Append to `openbot/infrastructure/persistence/dedup.py`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbot.application.ports.dedup import DedupPort

    # Structural check — fails type-checking if the API drifts.
    _witness: "DedupPort" = WebhookDedup(redis=None)
```

- [ ] **Step 4: Write `tests/_fakes/dedup.py`**

```python
"""FakeDedup — in-memory DedupPort for tests."""
from __future__ import annotations

from dataclasses import dataclass, field

from openbot.infrastructure.persistence.dedup import DedupOutcome


@dataclass
class FakeDedup:
    """First call per (channel, delivery_id) is FRESH; subsequent are DUPLICATE."""

    seen: set[tuple[str, str]] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def check_and_mark(
        self, channel: str, delivery_id: str
    ) -> DedupOutcome:
        self.calls.append((channel, delivery_id))
        if not delivery_id:
            return DedupOutcome.FRESH
        key = (channel, delivery_id)
        if key in self.seen:
            return DedupOutcome.DUPLICATE
        self.seen.add(key)
        return DedupOutcome.FRESH
```

- [ ] **Step 5: Write the contract test**

Create `tests/application/ports/test_dedup_port_contract.py`:

```python
"""Contract test — FakeDedup conforms to DedupPort."""
from __future__ import annotations

import pytest

from openbot.application.ports.dedup import DedupPort
from openbot.infrastructure.persistence.dedup import DedupOutcome
from tests._fakes.dedup import FakeDedup


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeDedup(), DedupPort)


@pytest.mark.asyncio
async def test_fresh_then_duplicate() -> None:
    d = FakeDedup()
    assert await d.check_and_mark("github", "delivery-1") == DedupOutcome.FRESH
    assert await d.check_and_mark("github", "delivery-1") == DedupOutcome.DUPLICATE
```

- [ ] **Step 6: Annotate the consumer**

In `openbot/entrypoints/api/app.py`, where `WebhookDedup` is constructed in `lifespan`, add an explicit Port annotation:

```python
from openbot.application.ports.dedup import DedupPort
from openbot.infrastructure.persistence.dedup import WebhookDedup
# ...
dedup: DedupPort = WebhookDedup(redis=redis)
app.state.dedup = dedup
```

The webhook route already reads `request.app.state.dedup` (set in Phase 1b Task 1.7); no signature change is needed at the call site.

- [ ] **Step 7: Run tests**

```bash
make check
```
Expected: 544 passed (543 + new contract test). `lint-imports` green (ignore list grew by exactly one allowed Port→leaf arrow).

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(ports): introduce DedupPort + FakeDedup contract test"
```

---

## Task 2.2: `QueuePort`

**Spec mapping:** `enqueue(redis, payload) -> str` is a free function in `infrastructure/queue/enqueue.py`. We wrap it in a stateful adapter class so callers depend on a Port instance, not on `redis` as a positional argument.

**Files:**
- Create: `openbot/application/ports/queue.py`
- Modify: `openbot/infrastructure/queue/enqueue.py` — add `RedisStreamQueue` class
- Modify: `openbot/entrypoints/api/app.py` — build & attach `app.state.queue`
- Modify: `openbot/entrypoints/api/routes/github_webhook.py` — call `request.app.state.queue.enqueue(payload)`
- Modify: `openbot/application/ports/__init__.py` — re-export
- Create: `tests/_fakes/queue.py`
- Create: `tests/application/ports/test_queue_port_contract.py`

- [ ] **Step 1: Write `application/ports/queue.py`**

```python
"""QueuePort — enqueue a parsed event for the worker."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from openbot.infrastructure.queue.payload import QueuePayload


@runtime_checkable
class QueuePort(Protocol):
    """Enqueue one payload onto the Redis Stream."""

    async def enqueue(self, payload: "QueuePayload") -> str:
        """Returns the Redis stream ID assigned to the entry."""
        ...
```

`QueuePayload` is an infra-shape DTO; the `TYPE_CHECKING` import keeps the runtime arrow direction clean.

Append to `openbot/application/ports/__init__.py`:

```python
from openbot.application.ports.queue import QueuePort  # noqa: F401
```

- [ ] **Step 2: Wrap the free function**

Edit `openbot/infrastructure/queue/enqueue.py`. After the existing `async def enqueue(redis, payload)`, append:

```python
class RedisStreamQueue:
    """Stateful QueuePort impl — owns one Redis client."""

    def __init__(self, redis: redis_async.Redis) -> None:
        self._redis = redis

    async def enqueue(self, payload: QueuePayload) -> str:
        return await enqueue(self._redis, payload)


if TYPE_CHECKING:
    from openbot.application.ports.queue import QueuePort

    _witness: "QueuePort" = RedisStreamQueue(redis=None)  # type: ignore[arg-type]
```

Keep the free `enqueue` function for back-compat across Phase 2 — Phase 3 deletes it.

- [ ] **Step 3: Switch the consumer**

In `openbot/entrypoints/api/app.py` lifespan:

```python
from openbot.infrastructure.queue.enqueue import RedisStreamQueue
# ...
app.state.queue = RedisStreamQueue(redis)
```

In `openbot/entrypoints/api/routes/github_webhook.py`, replace:

```python
from openbot.infrastructure.queue import QueuePayload, enqueue
# ...
sid = await enqueue(request.app.state.redis, payload)
```

with:

```python
from openbot.infrastructure.queue.payload import QueuePayload
# ...
sid = await request.app.state.queue.enqueue(payload)
```

- [ ] **Step 4: Write `tests/_fakes/queue.py`**

```python
"""FakeQueue — in-memory QueuePort. Each enqueue() returns a monotonic id."""
from __future__ import annotations

from dataclasses import dataclass, field

from openbot.infrastructure.queue.payload import QueuePayload


@dataclass
class FakeQueue:
    entries: list[QueuePayload] = field(default_factory=list)
    next_id: int = 0

    async def enqueue(self, payload: QueuePayload) -> str:
        self.entries.append(payload)
        sid = f"0-{self.next_id}"
        self.next_id += 1
        return sid
```

- [ ] **Step 5: Write the contract test**

Create `tests/application/ports/test_queue_port_contract.py`. The test needs a minimal `QueuePayload` — check `openbot/infrastructure/queue/payload.py` for the exact dataclass shape and use literal kwargs:

```python
"""Contract test — FakeQueue conforms to QueuePort."""
from __future__ import annotations

import pytest

from openbot.application.ports.queue import QueuePort
from openbot.infrastructure.queue.payload import QueuePayload
from tests._fakes.queue import FakeQueue


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeQueue(), QueuePort)


def _payload(delivery_id: str) -> QueuePayload:
    """Build a minimal QueuePayload — keep kwargs aligned with the dataclass."""
    # If QueuePayload's required fields change, update here.
    return QueuePayload(
        channel="github",
        delivery_id=delivery_id,
        event_json="{}",
        headers={},
    )


@pytest.mark.asyncio
async def test_enqueue_returns_unique_id() -> None:
    q = FakeQueue()
    a = await q.enqueue(_payload("a"))
    b = await q.enqueue(_payload("b"))
    assert a != b
    assert len(q.entries) == 2
```

If `QueuePayload` requires different kwargs, adjust `_payload` to match. Do NOT change the dataclass to fit the test.

- [ ] **Step 6: Run tests**

```bash
make check
```
Expected: 545 passed. `lint-imports` green.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(ports): introduce QueuePort + RedisStreamQueue adapter"
```

---

## Task 2.3: `ChannelAdapterPort`

**Spec mapping:** `infrastructure/adapters/base.ChannelAdapter` (an `ABC`) already encodes the contract. We define a structural `Protocol` whose shape matches it and prove satisfaction via `TYPE_CHECKING`.

**Files:**
- Create: `openbot/application/ports/channel_adapter.py`
- Modify: `openbot/infrastructure/adapters/base.py` — add `TYPE_CHECKING` witness
- Modify: `openbot/entrypoints/api/app.py` — annotate `adapter` build as `ChannelAdapterPort`
- Modify: `openbot/application/ports/__init__.py` — re-export
- Create: `tests/_fakes/channel_adapter.py`
- Create: `tests/application/ports/test_channel_adapter_port_contract.py`

- [ ] **Step 1: Write `application/ports/channel_adapter.py`**

```python
"""ChannelAdapterPort — channel-agnostic interaction surface."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from openbot.domain.events import UnifiedEvent


@runtime_checkable
class ChannelAdapterPort(Protocol):
    """One per channel. Currently only GitHub is implemented."""

    name: str

    def verify_signature(self, body: bytes, headers: "Mapping[str, str]") -> None:
        """Raise SignatureError on auth failure."""
        ...

    def parse_event(self, body: bytes, headers: "Mapping[str, str]") -> "UnifiedEvent":
        """Decode the authenticated payload."""
        ...

    async def reply(self, event: "UnifiedEvent", message: str) -> dict[str, Any]:
        """Post a reply on the originating thread."""
        ...
```

Append to `openbot/application/ports/__init__.py`:

```python
from openbot.application.ports.channel_adapter import ChannelAdapterPort  # noqa: F401
```

- [ ] **Step 2: Witness in infra**

Append to `openbot/infrastructure/adapters/base.py`:

```python
if TYPE_CHECKING:
    from openbot.application.ports.channel_adapter import ChannelAdapterPort

    # ABC fields satisfy the structural Port; pure type-check, no runtime cost.
    def _check_impl(adapter: ChannelAdapter) -> "ChannelAdapterPort":
        return adapter
```

(`TYPE_CHECKING` may already be imported — confirm before adding the import line.)

- [ ] **Step 3: Annotate the consumer**

In `openbot/entrypoints/api/app.py`, where `_build_auth(...)` returns the adapter:

```python
from openbot.application.ports.channel_adapter import ChannelAdapterPort
# ...
adapter: ChannelAdapterPort = _build_auth(settings)
app.state.adapter = adapter
```

- [ ] **Step 4: Write `tests/_fakes/channel_adapter.py`**

```python
"""FakeChannelAdapter — accepts every signature, records replies."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from openbot.domain.events import EventKind, UnifiedEvent


@dataclass
class FakeChannelAdapter:
    name: str = "fake"
    parsed_event: UnifiedEvent | None = None
    replies: list[tuple[str | None, str]] = field(default_factory=list)

    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> None:
        return  # always accept

    def parse_event(self, body: bytes, headers: Mapping[str, str]) -> UnifiedEvent:
        if self.parsed_event is None:
            # Build a minimal UnknownEvent — match the dataclass shape.
            return UnifiedEvent(kind=EventKind.UNKNOWN, channel=self.name)
        return self.parsed_event

    async def reply(self, event: UnifiedEvent, message: str) -> dict[str, Any]:
        self.replies.append((event.resource_key, message))
        return {"ok": True, "id": len(self.replies)}
```

If `UnifiedEvent` requires more kwargs than `kind` + `channel`, update the no-arg branch to pass the minimum needed for construction. Do NOT change `UnifiedEvent`.

- [ ] **Step 5: Write the contract test**

Create `tests/application/ports/test_channel_adapter_port_contract.py`:

```python
"""Contract test — FakeChannelAdapter conforms to ChannelAdapterPort."""
from __future__ import annotations

import pytest

from openbot.application.ports.channel_adapter import ChannelAdapterPort
from openbot.domain.events import EventKind, UnifiedEvent
from tests._fakes.channel_adapter import FakeChannelAdapter


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeChannelAdapter(), ChannelAdapterPort)


@pytest.mark.asyncio
async def test_reply_records_message() -> None:
    fa = FakeChannelAdapter()
    ev = fa.parse_event(b"", {})
    assert ev.kind == EventKind.UNKNOWN
    out = await fa.reply(ev, "hello")
    assert out["ok"] is True
    assert fa.replies == [(ev.resource_key, "hello")]
```

- [ ] **Step 6: Run tests**

```bash
make check
```
Expected: 546 passed. `lint-imports` green.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(ports): introduce ChannelAdapterPort + FakeChannelAdapter"
```

---

## End of Phase 2a

At this point:
- `application/ports/` holds 3 Protocol files (`dedup`, `queue`, `channel_adapter`).
- `tests/_fakes/` holds 3 fakes, each verified by a contract test.
- `app.state` carries Port-typed `dedup`, `queue`, `adapter` collaborators built once in lifespan.
- `entrypoints/api/routes/github_webhook.py` now calls `state.queue.enqueue(...)` instead of the free `enqueue(redis, ...)`.
- `make check` reports **546 passed** (543 baseline + 3 new contract tests).
- `.importlinter` has one new ignore line (the Port → leaf-enum exception for `DedupOutcome`).

Continue with [Phase 2b](2026-05-18-hexagonal-restructure-phase-2b-state-ports.md): `RunsRepoPort`, `ResourceLockPort`, `CancellationPort`, `AuditLogPort`.
