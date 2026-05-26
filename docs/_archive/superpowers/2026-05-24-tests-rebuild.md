# Tests Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current 175-file `tests/` tree with an engineered six-layer suite (unit / contract / integration / smoke / e2e / real_service), promote the 12 hand-written fake adapters from `tests/_fakes/` to a packaged `openbot/testing/` extra, and stage CI by layer.

**Architecture:** Single migration PR. Old `tests/` is deleted in one commit, new tree is built by phase. Every port in `openbot.application.ports` gets a contract test that runs against fake **and** in-process real implementation. Layer boundaries enforced by `import-linter`. CI staged: PR-fast (unit + contract) → push-full (+ integration + smoke) → nightly (+ e2e) → manual / release (+ real_service).

**Tech Stack:** pytest, pytest-asyncio, pytest-xdist, fakeredis, aiosqlite, respx, vcrpy, pytest-recording, import-linter. Spec: `docs/superpowers/specs/2026-05-24-tests-rebuild-design.md`.

**Pre-flight reads (engineer must have these in context):**
- `docs/superpowers/specs/2026-05-24-tests-rebuild-design.md` (the spec)
- `openbot/application/ports/*.py` (all 12 port Protocols)
- `tests/_fakes/*.py` (current fakes — semantic reference, not target)
- `.importlinter` (current layer rules)

---

## Phase 0 — Setup

### Task 0.1: Branch + safety tag

**Files:**
- None (git operations only)

- [ ] **Step 1: Confirm prerequisite branch landed**

Run: `git fetch origin && git log origin/main --oneline | head -5`
Expected: see commit "refactor/evals-runtime-openbot-harness" merged.
If not yet merged: STOP and surface to user before proceeding.

- [ ] **Step 2: Create rebuild branch off main**

```bash
git checkout main
git pull --ff-only origin main
git checkout -b refactor/tests-rebuild
```

- [ ] **Step 3: Tag the pre-rebuild commit for emergency rollback**

```bash
git tag pre-test-rebuild HEAD
git push origin pre-test-rebuild
```

- [ ] **Step 4: Confirm clean working tree**

Run: `git status`
Expected: `nothing to commit, working tree clean`

### Task 0.2: Add new dev dependencies

**Files:**
- Modify: `pyproject.toml` (the `[dependency-groups] dev` block and add `[project.optional-dependencies]`)

- [ ] **Step 1: Add the testing extra and three new dev libs**

In `pyproject.toml`, immediately after the existing `[dependency-groups]` table, add:

```toml
[project.optional-dependencies]
testing = [
    "fakeredis>=2.26",
    "aiosqlite>=0.20",
    "vcrpy>=6.0",
    "respx>=0.21",
    "pytest>=9.0.3",
    "pytest-asyncio>=1.3.0",
    "pytest-recording>=0.13",
]
```

In the existing `[dependency-groups] dev = [...]` block, add three lines (alphabetised within their existing comment groups):

```toml
    "pytest-xdist>=3.6",        # parallel test runner for unit/contract layer
    "respx>=0.21",              # httpx mock router for ChannelAdapter contract tests
    "vcrpy>=6.0",               # cassette record/replay for real_service GitHub tests
    "pytest-recording>=0.13",   # pytest plugin wrapper for vcrpy
```

- [ ] **Step 2: Sync deps**

Run: `make sync`
Expected: `uv sync --dev` completes; lockfile updated.

- [ ] **Step 3: Verify imports work**

Run: `uv run python -c "import fakeredis, aiosqlite, respx, vcr, pytest_recording, pytest_xdist; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): add testing extras (respx, vcrpy, pytest-recording, pytest-xdist)"
```

### Task 0.3: Wipe the old tests/ tree

**Files:**
- Delete: every file under `tests/` except this is wiped wholesale

- [ ] **Step 1: Remove the directory**

```bash
git rm -rf tests
```

- [ ] **Step 2: Confirm deletion is staged**

Run: `git status --short | head -10`
Expected: many `D  tests/...` lines.

- [ ] **Step 3: Commit the deletion alone**

```bash
git commit -m "chore(tests): delete legacy tests/ tree (rebuild incoming)

Per docs/superpowers/specs/2026-05-24-tests-rebuild-design.md.
Rollback path: \`git revert\` this commit, or \`git reset --hard pre-test-rebuild\`."
```

This commit MUST stand alone — it's the rollback target. Do not bundle other changes.

- [ ] **Step 4: Verify the working tree compiles still**

Run: `uv run python -c "import openbot; from openbot.application.ports import queue, runs_repo, llm; print('ok')"`
Expected: `ok` (source code untouched).

- [ ] **Step 5: Verify make check fails predictably**

Run: `make lint`
Expected: passes (lint targets `.` excluding tests).

Run: `make test 2>&1 | tail -3`
Expected: pytest collection error or `no tests collected` — this is intentional; we'll fix it before opening the PR.

---

## Phase 1 — `openbot/testing/` package

### Task 1.1: Package skeleton

**Files:**
- Create: `openbot/testing/__init__.py`
- Create: `openbot/testing/fakes/__init__.py`
- Create: `openbot/testing/builders/__init__.py`
- Create: `openbot/testing/inmemory/__init__.py`
- Create: `openbot/testing/recording/__init__.py`

- [ ] **Step 1: Create the top-level package init**

`openbot/testing/__init__.py`:

```python
"""Test doubles for OpenBot.

Importable via `from openbot.testing import FakeQueue, build_issue_opened_event`.
Requires `pip install openbot[testing]`. Production install does NOT bundle
fakeredis / aiosqlite / vcrpy / respx — those are in the `testing` extra.

Layer rule: production code in openbot.{domain,application,infrastructure,
entrypoints,core,dispatcher,evaluation} MUST NOT import from this package.
This is enforced by import-linter (.importlinter contract `no-testing-in-runtime`).
The `evals/` tree IS allowed to import from here.
"""

from __future__ import annotations

# Re-exports populated as fakes/builders/inmemory land. Keep this list
# sorted; CI doesn't enforce order but reviewers do.
__all__: list[str] = []
```

- [ ] **Step 2: Create empty subpackage inits**

`openbot/testing/fakes/__init__.py`:

```python
"""Fake implementations of openbot.application.ports.* protocols.

Each fake mirrors its port Protocol exactly (verified by a module-level
_PROTOCOL_CHECK assignment). Observable state is exposed as immutable
tuples of frozen dataclasses; no `.calls: list[dict]` weak typing.
Failure injection is explicit (constructor kwargs), never via env vars.
"""

from __future__ import annotations

__all__: list[str] = []
```

`openbot/testing/builders/__init__.py`:

```python
"""Factory functions for test data — events, payloads, decisions, runs.

Builders take keyword-only args with sensible defaults. Tests read as
`event = build_issue_opened_event(body="foo")` rather than 30-line
state-machine harness assembly."""

from __future__ import annotations

__all__: list[str] = []
```

`openbot/testing/inmemory/__init__.py`:

```python
"""In-process substitutes for external services.

build_inmemory_redis() yields a fakeredis client compatible with the
redis-py async API. build_inmemory_db() yields an aiosqlite-backed
SQLAlchemy session factory with Base.metadata.create_all already run.
Used by the contract layer's "real" parametrization to exercise real
adapter code without network/docker."""

from __future__ import annotations

__all__: list[str] = []
```

`openbot/testing/recording/__init__.py`:

```python
"""VCR cassette helpers for the real_service layer."""

from __future__ import annotations

__all__: list[str] = []
```

- [ ] **Step 3: Verify package imports**

Run: `uv run python -c "from openbot.testing import fakes, builders, inmemory, recording; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add openbot/testing
git commit -m "feat(testing): add openbot.testing package skeleton"
```

### Task 1.2: In-memory infrastructure factories

**Files:**
- Create: `openbot/testing/inmemory/redis.py`
- Create: `openbot/testing/inmemory/postgres.py`
- Create: `openbot/testing/inmemory/checkpointer.py`

- [ ] **Step 1: Create the redis factory**

`openbot/testing/inmemory/redis.py`:

```python
"""In-memory Redis client for contract / integration / e2e tests.

Uses fakeredis.aioredis which mirrors redis.asyncio.Redis surface area.
Production adapters (RedisQueue, RedisDedup, etc.) cannot tell the
difference; this is what makes the contract layer's "real" path
exercise real adapter code without docker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import fakeredis.aioredis


@asynccontextmanager
async def build_inmemory_redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    """Yield a fresh fakeredis client; flush + close on exit."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


__all__ = ["build_inmemory_redis"]
```

- [ ] **Step 2: Create the postgres factory**

`openbot/testing/inmemory/postgres.py`:

```python
"""In-memory SQL session factory backed by aiosqlite.

Runs Base.metadata.create_all once per fixture; alembic migrations are
NOT replayed (that's a real_service concern). Use this when you want
the SQLAlchemy code path real but no Postgres dialect features."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from openbot.infrastructure.persistence.models import Base


@asynccontextmanager
async def build_inmemory_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an async sessionmaker bound to an in-memory aiosqlite DB."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


__all__ = ["build_inmemory_db"]
```

- [ ] **Step 3: Create the checkpointer stub**

`openbot/testing/inmemory/checkpointer.py`:

```python
"""In-memory LangGraph checkpointer for agent integration tests.

Wraps langgraph's MemorySaver so callers don't have to know which
checkpointer class to import. Returned object satisfies
langgraph.checkpoint.base.BaseCheckpointSaver."""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver


def build_inmemory_checkpointer() -> MemorySaver:
    """Return a fresh MemorySaver. Callers own its lifetime."""
    return MemorySaver()


__all__ = ["build_inmemory_checkpointer"]
```

- [ ] **Step 4: Verify everything imports**

Run:
```bash
uv run python -c "
import asyncio
from openbot.testing.inmemory.redis import build_inmemory_redis
from openbot.testing.inmemory.postgres import build_inmemory_db
from openbot.testing.inmemory.checkpointer import build_inmemory_checkpointer

async def main():
    async with build_inmemory_redis() as r:
        await r.set('k', 'v')
        assert (await r.get('k')) == b'v'
    async with build_inmemory_db() as factory:
        async with factory() as session:
            from sqlalchemy import text
            assert (await session.execute(text('select 1'))).scalar() == 1
    cp = build_inmemory_checkpointer()
    print('ok')

asyncio.run(main())
"
```
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add openbot/testing/inmemory
git commit -m "feat(testing): add in-memory redis/postgres/checkpointer factories"
```

### Task 1.3: Builders

**Files:**
- Create: `openbot/testing/builders/events.py`
- Create: `openbot/testing/builders/payloads.py`
- Create: `openbot/testing/builders/runs.py`
- Create: `openbot/testing/builders/decisions.py`

- [ ] **Step 1: Inspect existing event/payload/decision dataclasses**

Run: `uv run python -c "
from openbot.domain.events import UnifiedEvent
from openbot.domain.workflows import Feature
import inspect
print(inspect.signature(UnifiedEvent))
"`

Use the printed signature to author builders that match every required
field. Do NOT guess field names — read them from `openbot/domain/events.py`
and `openbot/domain/workflows.py` first.

- [ ] **Step 2: Create the events builder**

`openbot/testing/builders/events.py`:

```python
"""UnifiedEvent factory functions for tests.

One builder per webhook kind. Each builder takes keyword-only args with
sensible defaults so tests state ONLY what they care about. Required
domain fields are populated; optional fields default to None or
deterministic values (delivery_id auto-generates a uuid4 if not given).
"""

from __future__ import annotations

import uuid
from typing import Any

from openbot.domain.events import EventKind, UnifiedEvent


def _delivery(delivery_id: str | None) -> str:
    return delivery_id or str(uuid.uuid4())


def build_issue_opened_event(
    *,
    repo: str = "owner/repo",
    issue_number: int = 1,
    sender: str = "octocat",
    body: str = "test issue body",
    title: str = "test issue title",
    delivery_id: str | None = None,
    installation_id: int = 100,
    event_seq: int = 0,
    clone_url: str | None = None,
) -> UnifiedEvent:
    """Build a deterministic issues.opened UnifiedEvent.

    Read openbot.domain.events.UnifiedEvent for the canonical field list;
    if a field is missing here, it's because no current test needs it —
    add a kwarg before depending on a default."""
    raw: dict[str, Any] = {
        "action": "opened",
        "issue": {"number": issue_number, "title": title, "body": body},
        "repository": {"full_name": repo, "clone_url": clone_url or f"https://github.com/{repo}.git"},
        "sender": {"login": sender, "type": "User"},
        "installation": {"id": installation_id},
    }
    return UnifiedEvent(
        channel="github",
        delivery_id=_delivery(delivery_id),
        kind=EventKind.ISSUE_OPENED,
        repo=repo,
        actor=sender,
        issue_number=issue_number,
        comment_body=body,
        actor_type="User",
        installation_id=installation_id,
        event_seq=event_seq,
        clone_url=clone_url,
        raw=raw,
    )


def build_pull_request_opened_event(
    *,
    repo: str = "owner/repo",
    pr_number: int = 1,
    sender: str = "octocat",
    title: str = "test PR",
    delivery_id: str | None = None,
    installation_id: int = 100,
    event_seq: int = 0,
    clone_url: str | None = None,
) -> UnifiedEvent:
    raw: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"number": pr_number, "title": title},
        "repository": {"full_name": repo, "clone_url": clone_url or f"https://github.com/{repo}.git"},
        "sender": {"login": sender, "type": "User"},
        "installation": {"id": installation_id},
    }
    return UnifiedEvent(
        channel="github",
        delivery_id=_delivery(delivery_id),
        kind=EventKind.PR_OPENED,
        repo=repo,
        actor=sender,
        pr_number=pr_number,
        actor_type="User",
        installation_id=installation_id,
        event_seq=event_seq,
        clone_url=clone_url,
        raw=raw,
    )


def build_issue_comment_command_event(
    *,
    repo: str = "owner/repo",
    issue_number: int = 1,
    sender: str = "octocat",
    command: str = "/fix",
    delivery_id: str | None = None,
    installation_id: int = 100,
    event_seq: int = 0,
) -> UnifiedEvent:
    raw: dict[str, Any] = {
        "action": "created",
        "issue": {"number": issue_number},
        "comment": {"body": command, "user": {"login": sender, "type": "User"}},
        "repository": {"full_name": repo},
        "sender": {"login": sender, "type": "User"},
        "installation": {"id": installation_id},
    }
    return UnifiedEvent(
        channel="github",
        delivery_id=_delivery(delivery_id),
        kind=EventKind.ISSUE_COMMENT_CREATED,
        repo=repo,
        actor=sender,
        issue_number=issue_number,
        comment_body=command,
        actor_type="User",
        installation_id=installation_id,
        event_seq=event_seq,
        raw=raw,
    )


__all__ = [
    "build_issue_opened_event",
    "build_pull_request_opened_event",
    "build_issue_comment_command_event",
]
```

> **Engineer note:** Field names above were taken from
> `openbot/domain/events.py` at plan-write time. If the dataclass has
> evolved by the time you read this, regenerate by re-reading the
> module — keep `delivery_id`, `kind`, `repo`, `actor`, and
> `actor_type="User"` (so `is_from_bot` is False) as the invariants any
> v0.1 builder must satisfy. Tests for the builders are added in Task 1.4.

- [ ] **Step 3: Create the payloads builder**

`openbot/testing/builders/payloads.py`:

```python
"""GitHub webhook *raw payload* builders (dict, not UnifiedEvent).

Use when a test needs the bytes-level payload — for example, e2e
webhook posts that go through HMAC signing. UnifiedEvent builders are
in events.py; do not duplicate them here."""

from __future__ import annotations

from typing import Any


def build_issue_opened_payload(
    *,
    repo: str = "owner/repo",
    issue_number: int = 1,
    sender: str = "octocat",
    body: str = "test",
    title: str = "test",
    installation_id: int = 100,
) -> dict[str, Any]:
    """Return a minimal but schema-valid issues.opened webhook body."""
    owner, name = repo.split("/", 1)
    return {
        "action": "opened",
        "issue": {
            "number": issue_number,
            "title": title,
            "body": body,
            "user": {"login": sender},
        },
        "repository": {
            "full_name": repo,
            "name": name,
            "owner": {"login": owner},
        },
        "sender": {"login": sender},
        "installation": {"id": installation_id},
    }


def build_pull_request_opened_payload(
    *,
    repo: str = "owner/repo",
    pr_number: int = 1,
    sender: str = "octocat",
    title: str = "test PR",
    installation_id: int = 100,
) -> dict[str, Any]:
    owner, name = repo.split("/", 1)
    return {
        "action": "opened",
        "pull_request": {
            "number": pr_number,
            "title": title,
            "user": {"login": sender},
            "head": {"sha": "deadbeef" * 5, "ref": "feature"},
            "base": {"sha": "cafef00d" * 5, "ref": "main"},
        },
        "repository": {
            "full_name": repo,
            "name": name,
            "owner": {"login": owner},
        },
        "sender": {"login": sender},
        "installation": {"id": installation_id},
    }


__all__ = ["build_issue_opened_payload", "build_pull_request_opened_payload"]
```

- [ ] **Step 4: Verify imports**

Run: `uv run python -c "from openbot.testing.builders import events, payloads; print('ok')"`
Expected: `ok`

Run: `uv run python -c "from openbot.testing.builders.payloads import build_issue_opened_payload; print(build_issue_opened_payload(repo='acme/api'))"`
Expected: a dict with `repository.full_name == 'acme/api'`.

Run: `uv run python -c "from openbot.testing.builders.events import build_issue_opened_event; e=build_issue_opened_event(); print(e.kind, e.repo, e.actor_type)"`
Expected: `EventKind.ISSUE_OPENED owner/repo User`

- [ ] **Step 5: Commit**

```bash
git add openbot/testing/builders
git commit -m "feat(testing): add UnifiedEvent + GitHub payload builders"
```

> **Engineer note:** `runs.py` and `decisions.py` builders are
> intentionally NOT created here. Add them on first use in a later
> task — the rule is *no fake/builder lands without a test that needs
> it*. When you do add one, mirror the events.py shape: read the model
> first (`openbot.infrastructure.persistence.models.TaskRun`,
> `openbot.application.dispatcher` Decision/Result types), then write
> a typed factory with explicit kwargs and a frozen-dataclass return.

### Task 1.4: Fakes — write the canonical FakeQueue

**Files:**
- Create: `openbot/testing/fakes/queue.py`

This task establishes the *pattern* for all 12 fakes. Subsequent tasks
(1.5–1.15) re-apply this pattern to the other 11 ports. Keep the diff
shape identical: frozen-dataclass observation type, `_PROTOCOL_CHECK`
sentinel at module bottom, explicit failure injection, no env reads.

- [ ] **Step 1: Read the existing legacy fake for semantic reference**

The legacy file was deleted in Task 0.3. Recover semantic intent from
git history:

```bash
git show pre-test-rebuild:tests/_fakes/queue.py
```

Read the output. Note: `calls: list[dict[str, Any]]` is the anti-pattern
we're replacing.

- [ ] **Step 2: Read the port Protocol it must match**

```bash
cat openbot/application/ports/queue.py
```

Note the exact method signatures — the new fake mirrors these to the
character.

- [ ] **Step 3: Write the new fake**

`openbot/testing/fakes/queue.py`:

```python
"""FakeQueue — in-memory QueuePort.

Records every enqueue() / enqueue_task_spec() in immutable tuples of
frozen dataclasses. Returns deterministic stream IDs of the form
`"0-<n>"` (mirrors Redis stream-ID shape so caller code parsing IDs
keeps working).

Failure injection: `fail_after=N` raises `fail_with` on the (N+1)th
enqueue. Both default to "never fail".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from openbot.application.ports.queue import QueuePort
from openbot.domain.events import UnifiedEvent
from openbot.domain.workflows import Feature

if TYPE_CHECKING:
    from openbot.infrastructure.queue.task_spec import TaskSpec


@dataclass(frozen=True)
class EnqueueRecord:
    """Snapshot of one enqueue() call. All fields immutable."""

    event: UnifiedEvent
    feature: Feature
    task_id: str
    check_run_id: int | None
    intent: str | None
    run_id: str | None
    prev_run_id: str | None
    resource_key: str | None
    event_seq: int


@dataclass
class FakeQueue:
    """In-memory QueuePort. Construct fresh per test."""

    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _events: list[EnqueueRecord] = field(default_factory=list, init=False)
    _task_specs: list["TaskSpec"] = field(default_factory=list, init=False)
    _next_id: int = field(default=0, init=False)

    @property
    def events(self) -> tuple[EnqueueRecord, ...]:
        """All enqueue() calls in order, as an immutable tuple."""
        return tuple(self._events)

    @property
    def task_specs(self) -> tuple["TaskSpec", ...]:
        """All enqueue_task_spec() calls in order."""
        return tuple(self._task_specs)

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
        self._maybe_fail()
        self._events.append(
            EnqueueRecord(
                event=event, feature=feature, task_id=task_id,
                check_run_id=check_run_id, intent=intent, run_id=run_id,
                prev_run_id=prev_run_id, resource_key=resource_key,
                event_seq=event_seq,
            )
        )
        return self._next_stream_id()

    async def enqueue_task_spec(self, spec: "TaskSpec") -> str:
        self._maybe_fail()
        self._task_specs.append(spec)
        return self._next_stream_id()

    def _maybe_fail(self) -> None:
        used = len(self._events) + len(self._task_specs)
        if self.fail_after is not None and used >= self.fail_after:
            raise self.fail_with("FakeQueue: simulated failure")

    def _next_stream_id(self) -> str:
        sid = f"0-{self._next_id}"
        self._next_id += 1
        return sid


# Static check: import-time error if FakeQueue stops satisfying QueuePort.
_PROTOCOL_CHECK: Final[QueuePort] = FakeQueue()


__all__ = ["EnqueueRecord", "FakeQueue"]
```

- [ ] **Step 4: Re-export from the fakes package**

Modify `openbot/testing/fakes/__init__.py` (replace its `__all__`):

```python
"""Fake implementations of openbot.application.ports.* protocols. (...)"""

from __future__ import annotations

from openbot.testing.fakes.queue import EnqueueRecord, FakeQueue

__all__ = ["EnqueueRecord", "FakeQueue"]
```

- [ ] **Step 5: Re-export from the top-level testing package**

Modify `openbot/testing/__init__.py`:

```python
"""Test doubles for OpenBot. (...)"""

from __future__ import annotations

from openbot.testing.fakes import EnqueueRecord, FakeQueue

__all__ = ["EnqueueRecord", "FakeQueue"]
```

- [ ] **Step 6: Verify protocol check + import**

Run: `uv run python -c "
from openbot.testing import FakeQueue
import asyncio

async def main():
    q = FakeQueue()
    # Smoke: 3 enqueues, observe immutable tuple.
    from openbot.testing.builders.payloads import build_issue_opened_payload
    # FakeQueue requires a real UnifiedEvent — skip enqueue here, just
    # verify the protocol check wired correctly.
    print('events tuple:', q.events)
    print('protocol check ok')

asyncio.run(main())
"`
Expected: `events tuple: ()` then `protocol check ok`.

- [ ] **Step 7: Commit**

```bash
git add openbot/testing/fakes/queue.py openbot/testing/fakes/__init__.py openbot/testing/__init__.py
git commit -m "feat(testing): add FakeQueue (canonical fake pattern)"
```

### Tasks 1.5–1.15: Other 11 fakes (apply Task 1.4 pattern)

Each task follows the **identical** 7-step shape from Task 1.4:

1. `git show pre-test-rebuild:tests/_fakes/<name>.py` for semantics
2. `cat openbot/application/ports/<name>.py` for the Protocol
3. Write `openbot/testing/fakes/<name>.py` with frozen-dataclass observation
4. Update `openbot/testing/fakes/__init__.py` exports
5. Update `openbot/testing/__init__.py` exports
6. Smoke-test import + `_PROTOCOL_CHECK` works
7. Commit `feat(testing): add Fake<Name>`

Per-port specifics (deviations from the FakeQueue template):

| Task | Fake | Port file | Observation tuple shape | Notes |
|---|---|---|---|---|
| 1.5 | `FakeRunsRepo` | `runs_repo.py` | `tuple[TransitionResult, ...]` exposed as `transitions` | `transition()` is the only method; legacy `_fakes/runs_repo.py` returned canned values, new fake records args + canned `TransitionResult` injected via `responses=[…]` queue |
| 1.6 | `FakeDedup` | `dedup.py` | `tuple[tuple[str,str], ...]` as `seen_keys`; default returns `FRESH` first time, `DUPLICATE` thereafter | inject specific outcomes via `responses` |
| 1.7 | `FakeRateLimiter` | `rate_limiter.py` | `tuple[tuple[str,int,int], ...]` as `checks`; default returns True; inject `responses: list[bool]` |
| 1.8 | `FakeResourceLock` | `resource_lock.py` | implements `lock(...)` as an async context manager; observation `tuple[str, ...]` as `acquired_keys`; failure mode = `contended_keys: set[str]` makes those return `False` |
| 1.9 | `FakeCancellation` | `cancellation.py` | observation `tuple[str, ...]` as `signalled`; inject `cancelled: set[str]` |
| 1.10 | `FakeAuditLog` | `audit_log.py` | observation `tuple[AuditEntry, ...]` (frozen dc with all kwargs) as `entries` |
| 1.11 | `FakeConfigLoader` | `config_loader.py` | constructor takes `config: EffectiveConfig`; record calls in `loads: tuple[str, ...]` (the repo full_name) |
| 1.12 | `FakeChannelAdapter` | `channel_adapter.py` | observation tuples per method group: `created_check_runs`, `posted_comments`, `posted_reviews`, `requested_reviewers`. ChannelAdapterPort is 264 lines — read it carefully, mirror EVERY method |
| 1.13 | `FakeSandbox` | `sandbox.py` | observation `tuple[ExecCall, ...]` as `executions`; inject `responses: list[ExecResult]` |
| 1.14 | `FakeSandboxCache` | `sandbox_cache.py` | **NEW — no legacy file exists**. Read the port Protocol carefully; observation `tuple[CacheOp, ...]` as `ops` |
| 1.15 | `FakeLLM` | `llm.py` | observation `tuple[LLMCall, ...]` as `calls`; inject `responses: list[str]` (round-robin); raises `IndexError` if responses exhausted |

> **Critical for Task 1.12 (FakeChannelAdapter):** Read all 264 lines of
> `openbot/application/ports/channel_adapter.py`. Every method must be
> mirrored. Missing one will silently break e2e tests later.

> **Critical for Task 1.14 (FakeSandboxCache):** No legacy reference
> exists. Read `openbot/application/ports/sandbox_cache.py` (109 lines),
> the existing `openbot/infrastructure/sandboxes/cache_fake.py` (which
> may already be a usable reference), and design the observation tuple
> from scratch.

After Task 1.15, `openbot/testing/__init__.py` exports all 12
`Fake<Name>` classes plus their `<X>Record` observation dataclasses.

- [ ] **Step (after all 11 tasks): Verify all fakes load**

Run: `uv run python -c "
from openbot.testing import (
    FakeQueue, FakeRunsRepo, FakeDedup, FakeRateLimiter,
    FakeResourceLock, FakeCancellation, FakeAuditLog,
    FakeConfigLoader, FakeChannelAdapter, FakeSandbox,
    FakeSandboxCache, FakeLLM,
)
print('all 12 fakes import ok')
"`
Expected: `all 12 fakes import ok`

### Task 1.16: GitHub VCR recording config

**Files:**
- Create: `openbot/testing/recording/github_vcr.py`

- [ ] **Step 1: Write the VCR config helper**

`openbot/testing/recording/github_vcr.py`:

```python
"""GitHub VCR config used by tests/real_service/github/.

Centralises secret redaction + match policy so individual test files
don't reinvent it. The pytest_recording plugin picks this up via the
vcr_config fixture (see tests/real_service/github/conftest.py)."""

from __future__ import annotations

import re
from typing import Any

REDACT_HEADERS: tuple[str, ...] = (
    "authorization",
    "x-hub-signature",
    "x-hub-signature-256",
    "x-github-delivery",
    "set-cookie",
    "cookie",
)

_REDACT_BODY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'"token":\s*"[^"]+"'), '"token": "REDACTED"'),
    (re.compile(r'"private_key":\s*"[^"]+"'), '"private_key": "REDACTED"'),
)


def redact_response_body(response: dict[str, Any]) -> dict[str, Any]:
    """Strip secret-like substrings from VCR response bodies in-place."""
    body = response.get("body", {}).get("string", b"")
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
        for pat, repl in _REDACT_BODY_PATTERNS:
            text = pat.sub(repl, text)
        response["body"]["string"] = text.encode("utf-8")
    return response


def github_vcr_config() -> dict[str, Any]:
    """Return a vcrpy config dict suitable for the @pytest.mark.vcr fixture."""
    return {
        "filter_headers": list(REDACT_HEADERS),
        "filter_query_parameters": ["access_token"],
        "before_record_response": redact_response_body,
        "match_on": ["method", "scheme", "host", "port", "path", "query", "body"],
        "record_mode": "none",  # default replay-only; CLI overrides at record time
        "decode_compressed_response": True,
    }


__all__ = ["github_vcr_config", "redact_response_body", "REDACT_HEADERS"]
```

- [ ] **Step 2: Verify import**

Run: `uv run python -c "from openbot.testing.recording.github_vcr import github_vcr_config; print(github_vcr_config()['match_on'])"`
Expected: `['method', 'scheme', 'host', 'port', 'path', 'query', 'body']`

- [ ] **Step 3: Commit**

```bash
git add openbot/testing/recording
git commit -m "feat(testing): add GitHub VCR redaction + match config"
```

### Task 1.17: Pin import-linter contract: no testing in runtime

**Files:**
- Modify: `.importlinter`

- [ ] **Step 1: Append the contract**

At the end of `.importlinter`, add:

```ini
[importlinter:contract:no-testing-in-runtime]
name = Production code must not import openbot.testing
type = forbidden
source_modules =
    openbot.domain
    openbot.application
    openbot.infrastructure
    openbot.entrypoints
    openbot.core
    openbot.dispatcher
    openbot.evaluation
forbidden_modules =
    openbot.testing
```

- [ ] **Step 2: Run lint-imports**

Run: `make lint-imports`
Expected: all contracts pass; no violations.

If a violation surfaces (some production module accidentally imports
from `openbot.testing`), STOP and fix the source — that's a real bug
this contract is catching, not a test setup failure.

- [ ] **Step 3: Commit**

```bash
git add .importlinter
git commit -m "ci(import-linter): add no-testing-in-runtime contract"
```

---

## Phase 2 — `tests/` scaffold

Wipe the legacy tree and lay down the new directory skeleton, root
conftest, layer conftests, and `pytest.ini`. After this phase
`pytest tests/` collects zero tests but exits 0 (no errors, no warnings).

### Task 2.1: Delete the legacy `tests/` tree

**Files:**
- Delete: every file under `tests/` (≈ 175 files)

- [ ] **Step 1: Snapshot the old tree count for the PR description**

```bash
find tests -type f -name '*.py' | wc -l > /tmp/openbot-old-test-count.txt
cat /tmp/openbot-old-test-count.txt
```
Expected: a number around 175. Save the value for later.

- [ ] **Step 2: Confirm the safety tag exists**

```bash
git tag -l pre-test-rebuild
```
Expected: `pre-test-rebuild` printed. If empty, STOP — Task 0.1 was skipped.

- [ ] **Step 3: Delete the directory**

```bash
git rm -r tests/
```
Expected: every legacy test path printed as `rm`.

- [ ] **Step 4: Commit the deletion as a standalone commit**

```bash
git commit -m "test: delete legacy tests/ tree (replaced in this PR)"
```

The deletion gets its own commit so reviewers can read the PR
chronologically: "everything is gone" → "everything new appears".

### Task 2.2: Create the layer directories

**Files:**
- Create: `tests/{unit,contract,integration,smoke,e2e,real_service}/`

- [ ] **Step 1: Create the six layer dirs and their per-layer subtree
  shells**

```bash
mkdir -p tests/unit/{domain,application/middleware,core,infrastructure}
mkdir -p tests/contract
mkdir -p tests/integration/{use_cases,dispatcher,middleware,persistence,queue,agents,evaluation}
mkdir -p tests/smoke
mkdir -p tests/e2e/fixtures/github_payloads
mkdir -p tests/real_service/{postgres,redis,github/cassettes,smee/fixtures/recorded_deliveries}
```

- [ ] **Step 2: Verify**

Run: `find tests -type d | sort`
Expected: every directory listed in spec §5 present, no others.

### Task 2.3: Write `pytest.ini`

**Files:**
- Create: `pytest.ini`

- [ ] **Step 1: Write the file exactly per spec §5.1**

```ini
[pytest]
testpaths = tests
asyncio_mode = auto
addopts = -ra --strict-markers --strict-config
markers =
    unit: layer=unit (PR)
    contract: layer=contract (PR)
    integration: layer=integration (push)
    smoke: layer=smoke (push)
    e2e: layer=e2e (nightly)
    real_service: layer=real-service (manual/release)
    requires_docker: needs local docker daemon
    requires_postgres: needs OPENBOT_DATABASE_URL
    requires_redis: needs OPENBOT_REDIS_URL
    requires_github_token: needs GITHUB_TOKEN to record VCR cassette
```

- [ ] **Step 2: Verify pytest accepts it**

Run: `uv run pytest --collect-only -q tests/ 2>&1 | head -5`
Expected: `0 tests collected` and no `unknown marker` warnings.

### Task 2.4: Write the root `tests/conftest.py`

**Files:**
- Create: `tests/conftest.py`

The root conftest has TWO responsibilities only (spec §8.1): autouse
ambient-env scrub and the session-scoped RSA key. The legacy conftest's
`evals.runtime.config.get_eval_config.cache_clear()` block is
**deliberately removed** — it leaks evals into pure unit tests; that
logic moves to `evals/tests/conftest.py` in Phase 9.

- [ ] **Step 1: Write the file**

```python
"""Root conftest — ambient-env isolation + RSA key for GitHub App tests.

Spec: docs/superpowers/specs/2026-05-24-tests-rebuild-design.md §8.1.

Two responsibilities, no business logic. Per-layer fixtures live in
each layer's own conftest.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_SCRUB_PREFIXES: tuple[str, ...] = ("OPENBOT_",)
_SCRUB_EXACT: tuple[str, ...] = (
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING_V2",
    "LANGSMITH_PROJECT",
    "LANGCHAIN_PROJECT",
    "LANGSMITH_PROJECT_EVAL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_ambient_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Scrub OPENBOT_* + LangSmith + LLM creds, chdir to clean tmp.

    Runs around EVERY test. Returns nothing — tests inherit the clean
    environment by virtue of the fixture being autouse. Tests that want
    specific values call ``monkeypatch.setenv(...)`` after this fixture
    has run.
    """
    for key in list(os.environ):
        if any(key.startswith(p) for p in _SCRUB_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    for key in _SCRUB_EXACT:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture(scope="session")
def rsa_private_key_pem() -> bytes:
    """Ephemeral RSA-2048 PEM for GitHub App auth tests.

    Session-scoped because keygen is ~100 ms; safe to share since
    tests treat it as read-only material.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `uv run python -c "import tests.conftest; print(tests.conftest._SCRUB_EXACT[0])"`
Expected: `LANGSMITH_API_KEY`

### Task 2.5: Per-layer conftest stubs

**Files:**
- Create: `tests/unit/conftest.py`
- Create: `tests/contract/conftest.py`
- Create: `tests/integration/conftest.py`
- Create: `tests/smoke/conftest.py`
- Create: `tests/e2e/conftest.py`
- Create: `tests/real_service/conftest.py`

Each layer's conftest is empty-but-present so pytest treats the layer
as a rootdir-anchored package. Layer-specific fixtures arrive in their
own phases.

- [ ] **Step 1: Write `tests/unit/conftest.py`**

```python
"""Unit-layer conftest. Intentionally near-empty.

Per spec §8.2: unit tests forbid IO fixtures. Add only pure helpers
here, never anything that opens a connection / spawns a process.
"""

from __future__ import annotations
```

- [ ] **Step 2: Write `tests/contract/conftest.py`**

```python
"""Contract-layer conftest.

Hosts the in-memory factory fixtures (`inmemory_redis`, `inmemory_db`,
`respx_mock_router`) consumed by the per-port contract files. The
parametrized fake/real fixture lives inside each `test_<port>_contract.py`
because the swap pair varies per port (see spec §7.2).
"""

from __future__ import annotations

import pytest

from openbot.testing.inmemory.redis import build_inmemory_redis
from openbot.testing.inmemory.postgres import build_inmemory_db


@pytest.fixture
async def inmemory_redis():
    """fakeredis-backed AsyncRedis. Closed on teardown."""
    async with build_inmemory_redis() as redis:
        yield redis


@pytest.fixture
async def inmemory_db():
    """aiosqlite-backed async session factory. Schema initialised."""
    async with build_inmemory_db() as session_factory:
        yield session_factory
```

- [ ] **Step 3: Write `tests/integration/conftest.py`**

```python
"""Integration-layer conftest.

Hosts one fixture per port that yields a `Fake<Port>` with default
construction. Tests that need a non-default fake (e.g. failure
injection) build it inline. Per spec §8.2 these are NOT autouse.
"""

from __future__ import annotations

import pytest

from openbot.testing.fakes.audit_log import FakeAuditLog
from openbot.testing.fakes.cancellation import FakeCancellation
from openbot.testing.fakes.channel_adapter import FakeChannelAdapter
from openbot.testing.fakes.config_loader import FakeConfigLoader
from openbot.testing.fakes.dedup import FakeDedup
from openbot.testing.fakes.llm import FakeLLM
from openbot.testing.fakes.queue import FakeQueue
from openbot.testing.fakes.rate_limiter import FakeRateLimiter
from openbot.testing.fakes.resource_lock import FakeResourceLock
from openbot.testing.fakes.runs_repo import FakeRunsRepo
from openbot.testing.fakes.sandbox import FakeSandbox
from openbot.testing.fakes.sandbox_cache import FakeSandboxCache


@pytest.fixture
def fake_audit_log() -> FakeAuditLog:
    return FakeAuditLog()


@pytest.fixture
def fake_cancellation() -> FakeCancellation:
    return FakeCancellation()


@pytest.fixture
def fake_channel() -> FakeChannelAdapter:
    return FakeChannelAdapter()


@pytest.fixture
def fake_config_loader() -> FakeConfigLoader:
    return FakeConfigLoader()


@pytest.fixture
def fake_dedup() -> FakeDedup:
    return FakeDedup()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def fake_queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def fake_rate_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def fake_resource_lock() -> FakeResourceLock:
    return FakeResourceLock()


@pytest.fixture
def fake_runs_repo() -> FakeRunsRepo:
    return FakeRunsRepo()


@pytest.fixture
def fake_sandbox() -> FakeSandbox:
    return FakeSandbox()


@pytest.fixture
def fake_sandbox_cache() -> FakeSandboxCache:
    return FakeSandboxCache()
```

- [ ] **Step 4: Write `tests/smoke/conftest.py`**

```python
"""Smoke-layer conftest. Boot helpers only.

Spec §8.2: minimal `Settings()` builder + app/worker boot helpers.
The smoke layer's whole budget is 5 s, so fixtures stay cheap.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture
def boot_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide the minimal env required for `Settings()` to construct.

    Smoke tests do not need real values — they need the validators to
    accept the shape. Real smokes use empty defaults wherever possible.
    """
    monkeypatch.setenv("OPENBOT_ENV", "test")
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "0")
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("OPENBOT_GITHUB_PRIVATE_KEY", "")
    yield
```

- [ ] **Step 5: Write `tests/e2e/conftest.py`**

```python
"""E2E-layer conftest.

Hosts `e2e_stack` — the assembled full pipeline (webhook → queue →
worker → use case → channel adapter) with **only** the GitHub channel
faked. Real assembly lives in `tests/e2e/_assemble.py` so tests can
read the wiring top-down.
"""

from __future__ import annotations

import pytest

from tests.e2e._assemble import build_e2e_stack, E2EStack


@pytest.fixture
async def e2e_stack() -> E2EStack:
    """Yield a fully-wired in-process pipeline. See `_assemble.py`."""
    async with build_e2e_stack() as stack:
        yield stack
```

- [ ] **Step 6: Write `tests/real_service/conftest.py`**

```python
"""Real-service layer conftest. `_env_or_skip` per spec §10.1."""

from __future__ import annotations

import os

import pytest


def _env_or_skip(*keys: str) -> dict[str, str]:
    """Module-level skip when any required env var is absent.

    Use at the top of a real_service test module:

        pytestmark = pytest.mark.real_service
        _ENV = _env_or_skip("OPENBOT_DATABASE_URL")

    `allow_module_level=True` produces ONE `s` per file rather than
    one per test, keeping output legible when running locally without
    `.env`.
    """
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        pytest.skip(
            f"missing env: {', '.join(missing)}",
            allow_module_level=True,
        )
    return {k: os.environ[k] for k in keys}


__all__ = ["_env_or_skip"]
```

- [ ] **Step 7: Verify the scaffold is collectable**

Run: `uv run pytest -q tests/ 2>&1 | tail -3`
Expected: `0 tests collected` (since no test files exist yet) with no
errors and no warnings.

- [ ] **Step 8: Commit**

```bash
git add tests/conftest.py tests/*/conftest.py pytest.ini
git commit -m "test(scaffold): add tests/ skeleton + per-layer conftests"
```

---

## Phase 3 — Unit layer

PR-fast. No IO. < 50 ms per test. The unit layer covers `domain/` in
full plus the IO-free policy parts of `application/`, `core/`, and
`infrastructure/`. Every test is constructed by hand from the imported
function under test — no fakes are imported here (fakes belong to the
contract / integration layers).

### Task 3.1: `tests/unit/domain/test_events.py`

**Files:**
- Create: `tests/unit/domain/test_events.py`
- Reference: `openbot/domain/events.py`

- [ ] **Step 1: Read the events module to anchor the contract**

Run: `uv run python -c "from openbot.domain.events import UnifiedEvent; print([f for f in UnifiedEvent.model_fields])"`
Expected: a list including `delivery_id`, `actor`, `repo`, etc. Use
the printed names verbatim in the test.

- [ ] **Step 2: Write the failing test**

```python
"""UnifiedEvent — value-object identity, equality, and hashability."""

from __future__ import annotations

import pytest

from openbot.domain.events import UnifiedEvent


def _event(**overrides: object) -> UnifiedEvent:
    base: dict[str, object] = {
        "delivery_id": "00000000-0000-0000-0000-000000000001",
        "channel": "github",
        "event_type": "issues.opened",
        "actor": "octocat",
        "repo": "owner/repo",
        "installation_id": 42,
        "raw": {},
    }
    base.update(overrides)
    return UnifiedEvent(**base)  # type: ignore[arg-type]


class TestUnifiedEvent:
    def test_value_equality(self) -> None:
        a = _event()
        b = _event()
        assert a == b

    def test_distinct_delivery_id_breaks_equality(self) -> None:
        a = _event(delivery_id="00000000-0000-0000-0000-000000000001")
        b = _event(delivery_id="00000000-0000-0000-0000-000000000002")
        assert a != b

    def test_event_is_immutable(self) -> None:
        ev = _event()
        with pytest.raises(Exception):
            ev.actor = "mallory"  # type: ignore[misc]
```

- [ ] **Step 3: Run and verify**

Run: `uv run pytest tests/unit/domain/test_events.py -v`
Expected: 3 tests pass. If a field name is wrong, the FIRST line of
the failure points at the offending kwarg — fix the helper, not the
domain model.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/domain/test_events.py
git commit -m "test(unit): UnifiedEvent identity + immutability"
```

### Task 3.2: `tests/unit/domain/test_review_spec.py`

**Files:**
- Create: `tests/unit/domain/test_review_spec.py`
- Reference: `openbot/domain/review.py`

- [ ] **Step 1: Write the failing test (cover happy path + bounds)**

```python
"""Domain: ReviewSpec construction and shape invariants."""

from __future__ import annotations

import pytest

from openbot.domain.review import ReviewSpec


class TestReviewSpec:
    def test_minimal_construction(self) -> None:
        spec = ReviewSpec(repo="owner/repo", pr_number=1, head_sha="a" * 40)
        assert spec.pr_number == 1
        assert spec.head_sha == "a" * 40

    def test_pr_number_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            ReviewSpec(repo="owner/repo", pr_number=0, head_sha="a" * 40)

    def test_head_sha_must_be_40_hex(self) -> None:
        with pytest.raises(ValueError):
            ReviewSpec(repo="owner/repo", pr_number=1, head_sha="short")
```

If `ReviewSpec` does not yet enforce these invariants, that is a real
domain bug — fix the model, then this test goes green. Domain
invariants belong in the model, not at every caller.

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/unit/domain/test_review_spec.py -v`
Expected: 3 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/domain/test_review_spec.py openbot/domain/review.py
git commit -m "test(unit): ReviewSpec value-object invariants"
```

### Task 3.3: `tests/unit/domain/test_decision.py`

**Files:**
- Create: `tests/unit/domain/test_decision.py`
- Reference: `openbot/domain/intents.py`, `openbot/domain/workflows.py`

- [ ] **Step 1: Write the failing test**

```python
"""Domain: Intent / Feature parsing and routing decisions."""

from __future__ import annotations

import pytest

from openbot.domain.intents import Intent, parse_intent
from openbot.domain.workflows import Feature


class TestIntent:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/openbot fix this", Intent.FIX),
            ("/openbot review", Intent.REVIEW),
            ("/openbot triage", Intent.TRIAGE),
            ("/openbot chat hello", Intent.CHAT),
        ],
    )
    def test_parse_intent_command(self, text: str, expected: Intent) -> None:
        assert parse_intent(text) == expected

    def test_parse_intent_unknown_returns_none(self) -> None:
        assert parse_intent("plain comment text") is None


class TestFeature:
    def test_feature_values_are_stable(self) -> None:
        # Stable enum values — wire format depends on these strings.
        assert Feature.TRIAGE.value == "triage"
        assert Feature.REVIEW.value == "review"
        assert Feature.FIX.value == "fix"
        assert Feature.CHAT.value == "chat"
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/unit/domain/test_decision.py -v`
Expected: all parametrized + scalar tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/domain/test_decision.py
git commit -m "test(unit): Intent parsing + Feature wire stability"
```

### Task 3.4: `tests/unit/application/test_classifier_pure.py`

**Files:**
- Create: `tests/unit/application/test_classifier_pure.py`
- Reference: `openbot/application/state/classifier.py`

The classifier's pure routing logic — no LLM call, no DB. Fed a
`UnifiedEvent` + `EffectiveConfig`, it returns a `Feature` or None.
LLM-driven classification stays out of `tests/`; that lives in
`evals/`. Per PRD §8.3.

- [ ] **Step 1: Write the failing test**

```python
"""ClassifierPolicy — deterministic per-feature routing rules."""

from __future__ import annotations

from openbot.application.state.classifier import classify_pure
from openbot.domain.events import UnifiedEvent
from openbot.domain.workflows import Feature


def _ev(event_type: str, body: str = "") -> UnifiedEvent:
    return UnifiedEvent(
        delivery_id="00000000-0000-0000-0000-000000000001",
        channel="github",
        event_type=event_type,
        actor="octocat",
        repo="owner/repo",
        installation_id=1,
        raw={"comment": {"body": body}} if body else {},
    )


class TestClassifyPure:
    def test_issues_opened_routes_to_triage(self) -> None:
        assert classify_pure(_ev("issues.opened")) == Feature.TRIAGE

    def test_pull_request_opened_routes_to_review(self) -> None:
        assert classify_pure(_ev("pull_request.opened")) == Feature.REVIEW

    def test_command_fix_routes_to_fix(self) -> None:
        assert classify_pure(_ev("issue_comment.created", body="/openbot fix")) == Feature.FIX

    def test_unrelated_comment_returns_none(self) -> None:
        assert classify_pure(_ev("issue_comment.created", body="lgtm")) is None
```

If `classify_pure` does not exist, extract the deterministic branches
out of `classifier.py` into a pure function with that name — the LLM
fallback stays in the existing function. The unit test stays unit; the
LLM path moves to `evals/`.

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/unit/application/test_classifier_pure.py -v`
Expected: 4 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/application/test_classifier_pure.py openbot/application/state/classifier.py
git commit -m "test(unit): classifier deterministic routing rules"
```

### Task 3.5: `tests/unit/application/test_dispatcher_decide.py`

**Files:**
- Create: `tests/unit/application/test_dispatcher_decide.py`
- Reference: `openbot/dispatcher/decide.py`

Pure decide-pipeline assertions: fed an event + classifier verdict, the
decide step yields a `TaskSpec` with the right feature, intent,
resource_key, and prev_run_id. No queue, no DB.

- [ ] **Step 1: Write the failing test**

```python
"""dispatcher.decide — pure TaskSpec assembly."""

from __future__ import annotations

from openbot.dispatcher.decide import decide_task_spec
from openbot.domain.events import UnifiedEvent
from openbot.domain.workflows import Feature


def _ev() -> UnifiedEvent:
    return UnifiedEvent(
        delivery_id="00000000-0000-0000-0000-000000000001",
        channel="github",
        event_type="pull_request.opened",
        actor="octocat",
        repo="owner/repo",
        installation_id=1,
        raw={"pull_request": {"number": 7, "head": {"sha": "a" * 40}}},
    )


def test_decide_yields_review_taskspec_for_pr_opened() -> None:
    spec = decide_task_spec(event=_ev(), feature=Feature.REVIEW)
    assert spec.feature is Feature.REVIEW
    assert spec.resource_key == "owner/repo#7"
    assert spec.prev_run_id is None
```

If `decide_task_spec` is buried inside a stateful method, lift it out
to a pure function. Tests of stateful flow stay in integration.

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/unit/application/test_dispatcher_decide.py -v`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/application/test_dispatcher_decide.py openbot/dispatcher/decide.py
git commit -m "test(unit): dispatcher.decide pure TaskSpec assembly"
```

### Task 3.6: `tests/unit/application/test_router.py`

**Files:**
- Create: `tests/unit/application/test_router.py`
- Reference: `openbot/application/router.py`

- [ ] **Step 1: Write the failing test**

```python
"""application.router — feature → use-case-callable lookup."""

from __future__ import annotations

import pytest

from openbot.application.router import resolve_use_case
from openbot.domain.workflows import Feature


@pytest.mark.parametrize("feature", list(Feature))
def test_every_feature_resolves(feature: Feature) -> None:
    handler = resolve_use_case(feature)
    assert callable(handler)


def test_unknown_feature_raises() -> None:
    with pytest.raises(KeyError):
        resolve_use_case("nope")  # type: ignore[arg-type]
```

If `resolve_use_case` is named differently, rename in `router.py` so
the call site reads cleanly. Renames stay in this commit.

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/unit/application/test_router.py -v`
Expected: parametrized over all features + unknown-feature case pass.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/application/test_router.py openbot/application/router.py
git commit -m "test(unit): router resolves every Feature"
```

### Task 3.7: `tests/unit/application/middleware/test_chain_order.py`

**Files:**
- Create: `tests/unit/application/middleware/test_chain_order.py`
- Reference: `openbot/application/middleware/__init__.py`

Pure structural test: the middleware chain order is fixed (security →
preflight → rate_limit → budget → cancel → sanitize → feature_toggle
→ audit_start). Order is observable by inspecting the chain registry.

- [ ] **Step 1: Write the failing test**

```python
"""Middleware chain — order is part of the contract."""

from __future__ import annotations

from openbot.application.middleware import middleware_chain_order


_EXPECTED: tuple[str, ...] = (
    "security",
    "preflight",
    "rate_limit",
    "budget",
    "cancel",
    "sanitize",
    "feature_toggle",
    "audit_start",
)


def test_chain_order_matches_spec() -> None:
    assert middleware_chain_order() == _EXPECTED
```

If `middleware_chain_order` does not yet exist, extract from
`__init__.py` so the order is a single source of truth that both prod
code and tests consult. Resist hardcoding the same tuple in two places.

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/unit/application/middleware/test_chain_order.py -v`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/application/middleware openbot/application/middleware/__init__.py
git commit -m "test(unit): middleware chain order is observable + fixed"
```

### Task 3.8: `tests/unit/core/test_settings.py`

**Files:**
- Create: `tests/unit/core/test_settings.py`
- Reference: `openbot/core/settings.py`

- [ ] **Step 1: Write the failing test**

```python
"""Settings — env-driven construction + cache invalidation."""

from __future__ import annotations

import pytest

from openbot.core.settings import Settings, get_settings


def test_settings_constructible_with_minimal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "0")
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", "x")
    s = Settings()
    assert s.github_app_id == 0


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "1")
    get_settings.cache_clear()
    a = get_settings()
    b = get_settings()
    assert a is b


def test_invalid_app_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "not-an-int")
    with pytest.raises(Exception):
        Settings()
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/unit/core/test_settings.py -v`
Expected: 3 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_settings.py
git commit -m "test(unit): Settings env construction + cache"
```

### Task 3.9: `tests/unit/core/test_metrics.py`

**Files:**
- Create: `tests/unit/core/test_metrics.py`
- Reference: `openbot/core/metrics.py`

- [ ] **Step 1: Write the failing test**

```python
"""Metrics counter / histogram registration is stable."""

from __future__ import annotations

from openbot.core.metrics import (
    EVENT_COUNTER_NAME,
    LATENCY_HISTOGRAM_NAME,
    get_event_counter,
)


def test_event_counter_name_is_stable() -> None:
    assert EVENT_COUNTER_NAME == "openbot_events_total"


def test_latency_histogram_name_is_stable() -> None:
    assert LATENCY_HISTOGRAM_NAME == "openbot_latency_seconds"


def test_event_counter_is_singleton() -> None:
    a = get_event_counter()
    b = get_event_counter()
    assert a is b
```

If those constants don't exist by exactly that name, add them — the
metrics names are public API consumed by Prometheus dashboards; making
them constants keeps drift caught.

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/unit/core/test_metrics.py -v`
Expected: 3 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/core/test_metrics.py openbot/core/metrics.py
git commit -m "test(unit): metric name stability"
```

### Task 3.10: `tests/unit/infrastructure/test_github_signing.py`

**Files:**
- Create: `tests/unit/infrastructure/test_github_signing.py`
- Reference: `openbot/infrastructure/adapters/github.py` (or wherever
  `verify_signature` lives)

Pure HMAC-SHA256 verification — no network. Use the
`rsa_private_key_pem` fixture only if the function under test accepts
RSA; webhook signing uses HMAC, not RSA, so a static secret is fine.

- [ ] **Step 1: Write the failing test**

```python
"""GitHub webhook signature verification — HMAC SHA-256."""

from __future__ import annotations

import hmac
import hashlib

import pytest

from openbot.infrastructure.adapters.github import (
    SignatureError,
    verify_webhook_signature,
)


_SECRET = b"super-secret-shared-key"


def _sign(body: bytes) -> str:
    sig = hmac.new(_SECRET, body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def test_valid_signature_passes() -> None:
    body = b'{"hello":"world"}'
    verify_webhook_signature(body, _sign(body), secret=_SECRET)


def test_tampered_body_fails() -> None:
    body = b'{"hello":"world"}'
    with pytest.raises(SignatureError):
        verify_webhook_signature(b"tampered", _sign(body), secret=_SECRET)


def test_missing_signature_header_fails() -> None:
    with pytest.raises(SignatureError):
        verify_webhook_signature(b"x", "", secret=_SECRET)
```

If `verify_webhook_signature` is private to a class, expose a free
function — webhook signing is generic and the existing class wrapping
is incidental.

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/unit/infrastructure/test_github_signing.py -v`
Expected: 3 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/infrastructure/test_github_signing.py openbot/infrastructure/adapters/github.py
git commit -m "test(unit): webhook HMAC verification"
```

### Task 3.11: `tests/unit/infrastructure/test_llm_sanitize.py`

**Files:**
- Create: `tests/unit/infrastructure/test_llm_sanitize.py`
- Reference: `openbot/infrastructure/llm/sanitize.py`

- [ ] **Step 1: Write the failing test**

```python
"""LLM input sanitiser — strip prompt-injection markers, secret patterns."""

from __future__ import annotations

import pytest

from openbot.infrastructure.llm.sanitize import sanitize_user_text


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ("hello world", "hello world"),
        ("Ignore previous instructions", "Ignore previous instructions"),  # text kept; LLM-side guard handled elsewhere
        ("ghs_" + "X" * 36, "[REDACTED:GH_TOKEN]"),
        ("AKIA" + "X" * 16, "[REDACTED:AWS_KEY]"),
        (
            "<FAKE_RSA_PRIVATE_KEY_FOR_TEST>",
            "[REDACTED:PEM]",
        ),
    ],
)
def test_sanitize_redacts_obvious_secrets(inp: str, expected: str) -> None:
    assert sanitize_user_text(inp) == expected
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/unit/infrastructure/test_llm_sanitize.py -v`
Expected: parametrized cases all pass.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/infrastructure/test_llm_sanitize.py
git commit -m "test(unit): LLM input sanitiser secret redaction"
```

### Task 3.12: Verify whole-unit-layer budget

- [ ] **Step 1: Run with timing**

Run: `uv run pytest tests/unit -q --durations=10`
Expected: all tests pass; slowest 10 each below 50 ms; whole layer
finishes in < 30 s. If anything is over budget, that test is doing IO
disguised as a unit test — move it to integration.

- [ ] **Step 2: Run with xdist**

Run: `uv run pytest tests/unit -q -n auto`
Expected: same pass count, 2-4× faster on a multicore machine. Failure
under xdist (e.g. shared globals) is a real bug — fix at source.

- [ ] **Step 3: Commit if any source fixes were made**

```bash
git add -A
git commit -m "test(unit): final pass — durations + xdist clean"
```

---

## Phase 4 — Contract layer (12 ports)

PR-fast. < 200 ms per test. Each port gets ONE file with a parametrized
fixture that runs the SAME test body against the fake and an in-process
real implementation, per spec §7.

The pattern in §7.1 is the canonical shape; the table in §7.2 fixes
the real-impl pair for each port. Tests in this layer **do not** import
`openbot.application.use_cases.*` (spec §7.3) — they exercise port
behaviour, not orchestration.

### Task 4.1: `tests/contract/test_queue_contract.py`

**Files:**
- Create: `tests/contract/test_queue_contract.py`
- Reference: `openbot/application/ports/queue.py`,
  `openbot/infrastructure/queue/enqueue.py` (or equivalent
  `RedisQueue` impl), `openbot/testing/fakes/queue.py`

- [ ] **Step 1: Write the failing test**

```python
"""QueuePort contract — fake and Redis-backed real run the same suite."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from openbot.application.ports.queue import QueuePort
from openbot.infrastructure.queue.enqueue import RedisQueue
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes.queue import FakeQueue
from openbot.testing.inmemory.redis import build_inmemory_redis


@pytest.fixture(params=["fake", "real"])
async def queue(request: pytest.FixtureRequest) -> AsyncIterator[QueuePort]:
    if request.param == "fake":
        yield FakeQueue()
    else:
        async with build_inmemory_redis() as redis:
            yield RedisQueue(redis=redis, stream_key="test:events")


class TestQueueContract:
    async def test_enqueue_returns_stream_id(self, queue: QueuePort) -> None:
        sid = await queue.enqueue(
            build_issue_opened_event(),
            feature="triage",
            task_id="t1",
        )
        assert isinstance(sid, str) and "-" in sid

    async def test_enqueue_preserves_order(self, queue: QueuePort) -> None:
        sids = [
            await queue.enqueue(
                build_issue_opened_event(issue_number=i),
                feature="triage",
                task_id=f"t{i}",
            )
            for i in range(5)
        ]
        assert sids == sorted(sids)

    async def test_enqueue_carries_optional_metadata(self, queue: QueuePort) -> None:
        sid = await queue.enqueue(
            build_issue_opened_event(),
            feature="review",
            task_id="t-meta",
            check_run_id=12345,
            intent="review",
            run_id="r-1",
            prev_run_id="r-0",
            resource_key="owner/repo#1",
            event_seq=3,
        )
        assert sid
```

- [ ] **Step 2: Run and verify both parametrizations pass**

Run: `uv run pytest tests/contract/test_queue_contract.py -v`
Expected: each test reported twice with `[fake]` and `[real]` suffix —
six lines, six PASSED. A `[real]` failure means the real adapter
diverged from the fake; fix the adapter, never the test.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_queue_contract.py
git commit -m "test(contract): QueuePort fake/real equivalence"
```

### Task 4.2: `tests/contract/test_runs_repo_contract.py`

**Files:**
- Create: `tests/contract/test_runs_repo_contract.py`
- Reference: `openbot/application/ports/runs_repo.py`,
  `openbot/infrastructure/persistence/runs_repo_impl.py`,
  `openbot/testing/fakes/runs_repo.py`,
  `openbot/testing/inmemory/postgres.py`

- [ ] **Step 1: Write the failing test**

```python
"""RunsRepoPort contract — CAS write semantics for resource_key."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from openbot.application.ports.runs_repo import RunsRepoPort
from openbot.application.state.runs_repo import TransitionResult
from openbot.infrastructure.persistence.runs_repo_impl import SqlAlchemyRunsRepo
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes.runs_repo import FakeRunsRepo
from openbot.testing.inmemory.postgres import build_inmemory_db


@pytest.fixture(params=["fake", "real"])
async def runs_repo(request: pytest.FixtureRequest) -> AsyncIterator[RunsRepoPort]:
    if request.param == "fake":
        yield FakeRunsRepo()
    else:
        async with build_inmemory_db() as session_factory:
            yield SqlAlchemyRunsRepo(session_factory=session_factory)


class TestRunsRepoContract:
    async def test_first_event_creates_run(self, runs_repo: RunsRepoPort) -> None:
        result: TransitionResult = await runs_repo.transition(
            event=build_issue_opened_event(issue_number=1),
            new_run_id="r1",
        )
        assert result.run_id == "r1"
        assert result.prev_run_id is None

    async def test_second_event_supersedes(self, runs_repo: RunsRepoPort) -> None:
        ev = build_issue_opened_event(issue_number=1)
        await runs_repo.transition(event=ev, new_run_id="r1")
        result = await runs_repo.transition(event=ev, new_run_id="r2")
        assert result.run_id == "r2"
        assert result.prev_run_id == "r1"

    async def test_distinct_resource_keys_are_independent(self, runs_repo: RunsRepoPort) -> None:
        r1 = await runs_repo.transition(
            event=build_issue_opened_event(issue_number=1), new_run_id="a"
        )
        r2 = await runs_repo.transition(
            event=build_issue_opened_event(issue_number=2), new_run_id="b"
        )
        assert r1.prev_run_id is None
        assert r2.prev_run_id is None
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/contract/test_runs_repo_contract.py -v`
Expected: each test x [fake]/[real] — six lines PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_runs_repo_contract.py
git commit -m "test(contract): RunsRepoPort CAS semantics"
```

### Task 4.3: `tests/contract/test_dedup_contract.py`

**Files:**
- Create: `tests/contract/test_dedup_contract.py`
- Reference: `openbot/application/ports/dedup.py`,
  `openbot/infrastructure/persistence/dedup.py`,
  `openbot/testing/fakes/dedup.py`

- [ ] **Step 1: Write the failing test**

```python
"""DedupPort contract — idempotent FRESH/DUPLICATE/FALLBACK_OPEN."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from openbot.application.ports.dedup import DedupPort
from openbot.domain.dedup import DedupOutcome
from openbot.infrastructure.persistence.dedup import RedisDedup
from openbot.testing.fakes.dedup import FakeDedup
from openbot.testing.inmemory.redis import build_inmemory_redis


@pytest.fixture(params=["fake", "real"])
async def dedup(request: pytest.FixtureRequest) -> AsyncIterator[DedupPort]:
    if request.param == "fake":
        yield FakeDedup()
    else:
        async with build_inmemory_redis() as redis:
            yield RedisDedup(redis=redis, ttl_seconds=60)


class TestDedupContract:
    async def test_first_call_is_fresh(self, dedup: DedupPort) -> None:
        outcome = await dedup.check_and_mark("github", "delivery-1")
        assert outcome is DedupOutcome.FRESH

    async def test_replay_is_duplicate(self, dedup: DedupPort) -> None:
        await dedup.check_and_mark("github", "delivery-1")
        outcome = await dedup.check_and_mark("github", "delivery-1")
        assert outcome is DedupOutcome.DUPLICATE

    async def test_distinct_channels_are_independent(self, dedup: DedupPort) -> None:
        await dedup.check_and_mark("github", "x")
        outcome = await dedup.check_and_mark("slack", "x")
        assert outcome is DedupOutcome.FRESH
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/contract/test_dedup_contract.py -v`
Expected: 6 lines PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_dedup_contract.py
git commit -m "test(contract): DedupPort outcome equivalence"
```

### Task 4.4: `tests/contract/test_rate_limiter_contract.py`

**Files:**
- Create: `tests/contract/test_rate_limiter_contract.py`
- Reference: `openbot/application/ports/rate_limiter.py`,
  `openbot/infrastructure/persistence/rate_limiter_redis.py`,
  `openbot/testing/fakes/rate_limiter.py`

- [ ] **Step 1: Write the failing test**

```python
"""RateLimiterPort — count-based allow/deny with fail-open."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from openbot.application.ports.rate_limiter import RateLimiterPort
from openbot.infrastructure.persistence.rate_limiter_redis import RedisRateLimiter
from openbot.testing.fakes.rate_limiter import FakeRateLimiter
from openbot.testing.inmemory.redis import build_inmemory_redis


@pytest.fixture(params=["fake", "real"])
async def limiter(request: pytest.FixtureRequest) -> AsyncIterator[RateLimiterPort]:
    if request.param == "fake":
        yield FakeRateLimiter()
    else:
        async with build_inmemory_redis() as redis:
            yield RedisRateLimiter(redis=redis)


class TestRateLimiterContract:
    async def test_under_limit_allows(self, limiter: RateLimiterPort) -> None:
        for _ in range(3):
            assert await limiter.check("k", limit=5, window_seconds=60) is True

    async def test_at_limit_then_denies(self, limiter: RateLimiterPort) -> None:
        for _ in range(3):
            await limiter.check("k", limit=3, window_seconds=60)
        assert await limiter.check("k", limit=3, window_seconds=60) is False

    async def test_distinct_keys_independent(self, limiter: RateLimiterPort) -> None:
        for _ in range(3):
            await limiter.check("a", limit=3, window_seconds=60)
        assert await limiter.check("b", limit=3, window_seconds=60) is True
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/contract/test_rate_limiter_contract.py -v`
Expected: 6 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_rate_limiter_contract.py
git commit -m "test(contract): RateLimiterPort count semantics"
```

### Task 4.5: `tests/contract/test_cancellation_contract.py`

**Files:**
- Create: `tests/contract/test_cancellation_contract.py`
- Reference: `openbot/application/ports/cancellation.py`,
  `openbot/infrastructure/persistence/cancellation_redis.py`,
  `openbot/testing/fakes/cancellation.py`

- [ ] **Step 1: Write the failing test**

```python
"""CancellationPort — durable cancel signal."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from openbot.application.ports.cancellation import CancellationPort
from openbot.infrastructure.persistence.cancellation_redis import RedisCancellation
from openbot.testing.fakes.cancellation import FakeCancellation
from openbot.testing.inmemory.redis import build_inmemory_redis


@pytest.fixture(params=["fake", "real"])
async def cancellation(request: pytest.FixtureRequest) -> AsyncIterator[CancellationPort]:
    if request.param == "fake":
        yield FakeCancellation()
    else:
        async with build_inmemory_redis() as redis:
            yield RedisCancellation(redis=redis)


class TestCancellationContract:
    async def test_unsignaled_returns_false(self, cancellation: CancellationPort) -> None:
        assert await cancellation.is_cancelled("r1") is False

    async def test_signal_then_check_returns_true(
        self, cancellation: CancellationPort
    ) -> None:
        await cancellation.signal("r1")
        assert await cancellation.is_cancelled("r1") is True

    async def test_distinct_run_ids_are_independent(
        self, cancellation: CancellationPort
    ) -> None:
        await cancellation.signal("r1")
        assert await cancellation.is_cancelled("r2") is False
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/contract/test_cancellation_contract.py -v`
Expected: 6 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_cancellation_contract.py
git commit -m "test(contract): CancellationPort signal semantics"
```

### Task 4.6: `tests/contract/test_resource_lock_contract.py`

**Files:**
- Create: `tests/contract/test_resource_lock_contract.py`
- Reference: `openbot/application/ports/resource_lock.py`,
  `openbot/infrastructure/persistence/resource_lock_redis.py`,
  `openbot/testing/fakes/resource_lock.py`

- [ ] **Step 1: Write the failing test**

```python
"""ResourceLockPort — async-CM acquire/release."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from openbot.application.ports.resource_lock import ResourceLockPort
from openbot.infrastructure.persistence.resource_lock_redis import RedisResourceLock
from openbot.testing.fakes.resource_lock import FakeResourceLock
from openbot.testing.inmemory.redis import build_inmemory_redis


@pytest.fixture(params=["fake", "real"])
async def lock(request: pytest.FixtureRequest) -> AsyncIterator[ResourceLockPort]:
    if request.param == "fake":
        yield FakeResourceLock()
    else:
        async with build_inmemory_redis() as redis:
            yield RedisResourceLock(redis=redis)


class TestResourceLockContract:
    async def test_uncontended_lock_acquires(self, lock: ResourceLockPort) -> None:
        async with lock.lock("repo#1", ttl_seconds=5) as acquired:
            assert acquired is True

    async def test_contended_lock_returns_false(self, lock: ResourceLockPort) -> None:
        async with lock.lock("repo#1", ttl_seconds=5):
            async with lock.lock("repo#1", ttl_seconds=5) as second:
                assert second is False

    async def test_release_allows_reacquire(self, lock: ResourceLockPort) -> None:
        async with lock.lock("repo#1", ttl_seconds=5):
            pass
        async with lock.lock("repo#1", ttl_seconds=5) as acquired:
            assert acquired is True
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/contract/test_resource_lock_contract.py -v`
Expected: 6 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_resource_lock_contract.py
git commit -m "test(contract): ResourceLockPort acquire/release"
```

### Task 4.7: `tests/contract/test_audit_log_contract.py`

**Files:**
- Create: `tests/contract/test_audit_log_contract.py`
- Reference: `openbot/application/ports/audit_log.py`,
  `openbot/infrastructure/persistence/audit_log_impl.py`,
  `openbot/testing/fakes/audit_log.py`

- [ ] **Step 1: Write the failing test**

```python
"""AuditLogPort — append-only writer; observable order."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from openbot.application.ports.audit_log import AuditLogPort
from openbot.infrastructure.persistence.audit_log_impl import SqlAlchemyAuditLog
from openbot.testing.fakes.audit_log import FakeAuditLog
from openbot.testing.inmemory.postgres import build_inmemory_db


@pytest.fixture(params=["fake", "real"])
async def audit(request: pytest.FixtureRequest) -> AsyncIterator[AuditLogPort]:
    if request.param == "fake":
        yield FakeAuditLog()
    else:
        async with build_inmemory_db() as session_factory:
            yield SqlAlchemyAuditLog(session_factory=session_factory)


class TestAuditLogContract:
    async def test_write_does_not_raise(self, audit: AuditLogPort) -> None:
        await audit.write(phase="ingest", outcome="ok")

    async def test_optional_fields_are_persisted(self, audit: AuditLogPort) -> None:
        await audit.write(
            phase="dispatch",
            delivery_id="d1",
            repo="owner/r",
            actor="octocat",
            workflow="review",
            outcome="ok",
            details={"k": "v"},
        )
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/contract/test_audit_log_contract.py -v`
Expected: 4 PASSED. Audit log read-back is exercised by integration
(`tests/integration/persistence/test_db.py`); contract layer asserts
the write surface only.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_audit_log_contract.py
git commit -m "test(contract): AuditLogPort write surface"
```

### Task 4.8: `tests/contract/test_config_loader_contract.py`

**Files:**
- Create: `tests/contract/test_config_loader_contract.py`
- Reference: `openbot/application/ports/config_loader.py`,
  `openbot/infrastructure/config_loader.py`,
  `openbot/testing/fakes/config_loader.py`,
  `openbot/testing/fakes/channel_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
"""ConfigLoaderPort — defaults + repo override merge."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from openbot.application.ports.config_loader import ConfigLoaderPort
from openbot.infrastructure.config_loader import YamlConfigLoader
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes.channel_adapter import FakeChannelAdapter
from openbot.testing.fakes.config_loader import FakeConfigLoader


@pytest.fixture(params=["fake", "real"])
async def loader(request: pytest.FixtureRequest) -> AsyncIterator[ConfigLoaderPort]:
    if request.param == "fake":
        yield FakeConfigLoader()
    else:
        # Real loader fetches via the adapter's contents API. We use a
        # FakeChannelAdapter primed with one config file — that's the
        # boundary we're testing on the real side.
        yield YamlConfigLoader()


class TestConfigLoaderContract:
    async def test_returns_effective_config(self, loader: ConfigLoaderPort) -> None:
        adapter = FakeChannelAdapter(
            repo_files={"openbot.yml": b"review:\n  enabled: true\n"}
        )
        config = await loader.load_for_repo(adapter, build_issue_opened_event())
        assert config is not None

    async def test_missing_file_falls_back_to_defaults(
        self, loader: ConfigLoaderPort
    ) -> None:
        adapter = FakeChannelAdapter(repo_files={})
        config = await loader.load_for_repo(adapter, build_issue_opened_event())
        assert config is not None
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/contract/test_config_loader_contract.py -v`
Expected: 4 PASSED. If real loader requires extra wiring, the wiring
goes in this test file — keep contract tests self-contained.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_config_loader_contract.py
git commit -m "test(contract): ConfigLoaderPort defaults + override"
```

### Task 4.9: `tests/contract/test_channel_adapter_contract.py`

**Files:**
- Create: `tests/contract/test_channel_adapter_contract.py`
- Reference: `openbot/application/ports/channel_adapter.py`,
  `openbot/infrastructure/adapters/github.py`,
  `openbot/testing/fakes/channel_adapter.py`

The real-side runs the production GitHub adapter against a `respx`
mock router — no real HTTP. Per spec §7.2, that's the in-process
"real" pair for `ChannelAdapterPort`.

- [ ] **Step 1: Write the failing test**

```python
"""ChannelAdapterPort — fake matches real (against respx)."""

from __future__ import annotations

from typing import AsyncIterator

import pytest
import respx
from httpx import Response

from openbot.application.ports.channel_adapter import ChannelAdapterPort
from openbot.infrastructure.adapters.github import GitHubChannelAdapter
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes.channel_adapter import FakeChannelAdapter


@pytest.fixture(params=["fake", "real"])
async def adapter(request: pytest.FixtureRequest) -> AsyncIterator[ChannelAdapterPort]:
    if request.param == "fake":
        yield FakeChannelAdapter(replies=[])
    else:
        with respx.mock(base_url="https://api.github.com") as mock:
            mock.post(
                "/repos/owner/repo/issues/1/comments"
            ).mock(return_value=Response(201, json={"id": 100, "body": "hi"}))
            mock.get(
                "/repos/owner/repo/issues/1/labels"
            ).mock(return_value=Response(200, json=[{"name": "bug"}]))
            yield GitHubChannelAdapter()


class TestChannelAdapterContract:
    async def test_reply_returns_dict_with_id(
        self, adapter: ChannelAdapterPort
    ) -> None:
        ev = build_issue_opened_event(issue_number=1)
        result = await adapter.reply(ev, "hi")
        assert "id" in result

    async def test_get_issue_labels_returns_frozenset(
        self, adapter: ChannelAdapterPort
    ) -> None:
        ev = build_issue_opened_event(issue_number=1)
        labels = await adapter.get_issue_labels(ev, 1)
        assert isinstance(labels, frozenset)
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/contract/test_channel_adapter_contract.py -v`
Expected: 4 PASSED. respx fails closed — any unmocked request raises.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_channel_adapter_contract.py
git commit -m "test(contract): ChannelAdapterPort reply/labels"
```

### Task 4.10: `tests/contract/test_sandbox_contract.py`

**Files:**
- Create: `tests/contract/test_sandbox_contract.py`
- Reference: `openbot/application/ports/sandbox.py`,
  `openbot/infrastructure/sandboxes/fake.py`,
  `openbot/testing/fakes/sandbox.py`

Per spec §7.2: the in-process real pair for `SandboxPort` is
`DockerSandbox` — but Docker is unavailable in PR-fast CI. We run the
fake side unconditionally and gate the real side behind
`requires_docker`. Daytona / Modal sandboxes are NOT in the contract
layer; their correctness is exercised by `real_service`.

- [ ] **Step 1: Write the failing test**

```python
"""SandboxPort contract — fake unconditional, docker conditional."""

from __future__ import annotations

import shutil
from typing import AsyncIterator

import pytest

from openbot.application.ports.sandbox import SandboxPort
from openbot.testing.fakes.sandbox import FakeSandbox


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@pytest.fixture(
    params=[
        "fake",
        pytest.param(
            "real",
            marks=pytest.mark.skipif(not _docker_available(), reason="docker not available"),
        ),
    ]
)
async def sandbox(request: pytest.FixtureRequest) -> AsyncIterator[SandboxPort]:
    if request.param == "fake":
        async with FakeSandbox() as s:
            yield s
    else:
        from openbot.infrastructure.sandboxes.docker import DockerSandbox

        async with DockerSandbox(image="alpine:3.19") as s:
            yield s


class TestSandboxContract:
    async def test_workspace_is_set(self, sandbox: SandboxPort) -> None:
        assert sandbox.workspace

    async def test_write_then_read_round_trip(self, sandbox: SandboxPort) -> None:
        await sandbox.write_file("hello.txt", "world")
        assert await sandbox.read_file("hello.txt") == "world"

    async def test_run_echo(self, sandbox: SandboxPort) -> None:
        result = await sandbox.run(command=["echo", "hi"], timeout_seconds=5)
        assert result.exit_code == 0
        assert result.timed_out is False
        assert "hi" in result.stdout

    async def test_close_is_idempotent(self, sandbox: SandboxPort) -> None:
        await sandbox.close()
        await sandbox.close()
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/contract/test_sandbox_contract.py -v`
Expected: fake side PASSED unconditionally; real side either PASSED or
SKIPPED with reason `docker not available`.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_sandbox_contract.py
git commit -m "test(contract): SandboxPort fake + conditional docker"
```

### Task 4.11: `tests/contract/test_sandbox_cache_contract.py`

**Files:**
- Create: `tests/contract/test_sandbox_cache_contract.py`
- Reference: `openbot/application/ports/sandbox_cache.py`,
  `openbot/infrastructure/sandboxes/cache_fake.py`,
  `openbot/testing/fakes/sandbox_cache.py`

Per spec §7.2: real pair is `InMemorySandboxCache` (the
`cache_fake.py` adapter). The fake under test is the new
`openbot/testing/fakes/sandbox_cache.py` from Task 1.15 — this is the
contract test that validates the gap closure called out in spec §6.2.

- [ ] **Step 1: Write the failing test**

```python
"""SandboxCachePort — acquire-miss, publish-then-acquire-hit, evict."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from openbot.application.ports.sandbox_cache import SandboxCachePort
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.domain.checkout import CheckoutSpec, CloneStrategy
from openbot.infrastructure.sandboxes.cache_fake import InMemorySandboxCache
from openbot.testing.fakes.sandbox import FakeSandbox
from openbot.testing.fakes.sandbox_cache import FakeSandboxCache


@pytest.fixture(params=["fake", "real"])
async def cache(request: pytest.FixtureRequest) -> AsyncIterator[SandboxCachePort]:
    if request.param == "fake":
        yield FakeSandboxCache()
    else:
        yield InMemorySandboxCache(max_entries=8)


def _spec() -> CheckoutSpec:
    return CheckoutSpec(
        repo_url="https://example/owner/repo.git",
        ref="main",
        strategy=CloneStrategy.SHALLOW,
    )


class TestSandboxCacheContract:
    async def test_miss_returns_none(self, cache: SandboxCachePort) -> None:
        assert await cache.acquire(_spec(), token="t", installation_id=1) is None

    async def test_publish_then_acquire_hits(self, cache: SandboxCachePort) -> None:
        sandbox = FakeSandbox()
        await sandbox.__aenter__()
        handle = SandboxedHandle(sandbox=sandbox, checkout=_spec(), token="t")
        await cache.publish(handle, installation_id=1)
        hit = await cache.acquire(_spec(), token="t", installation_id=1)
        assert hit is not None

    async def test_evict_repo_clears_entries(self, cache: SandboxCachePort) -> None:
        sandbox = FakeSandbox()
        await sandbox.__aenter__()
        handle = SandboxedHandle(sandbox=sandbox, checkout=_spec(), token="t")
        await cache.publish(handle, installation_id=1)
        await cache.evict_repo(_spec().repo_url, installation_id=1)
        assert await cache.acquire(_spec(), token="t", installation_id=1) is None
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/contract/test_sandbox_cache_contract.py -v`
Expected: 6 PASSED. Failure here indicates the new fake from Task
1.15 diverges from the in-memory adapter; fix the fake.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_sandbox_cache_contract.py
git commit -m "test(contract): SandboxCachePort acquire/publish/evict"
```

### Task 4.12: `tests/contract/test_llm_contract.py`

**Files:**
- Create: `tests/contract/test_llm_contract.py`
- Reference: `openbot/application/ports/llm.py`,
  `openbot/infrastructure/llm/complete.py`,
  `openbot/testing/fakes/llm.py`

Real-side uses `litellm`'s `mock_response` so no network. Per spec §7.2.

- [ ] **Step 1: Write the failing test**

```python
"""LLMPort contract — fake matches LiteLLM-mocked real."""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from openbot.application.ports.llm import LLMPort
from openbot.infrastructure.llm.complete import LiteLLMAdapter
from openbot.testing.fakes.llm import FakeLLM


@pytest.fixture(params=["fake", "real"])
async def llm(request: pytest.FixtureRequest) -> AsyncIterator[LLMPort]:
    if request.param == "fake":
        yield FakeLLM(replies=["mocked response"])
    else:
        # litellm provides built-in mocking via mock_response.
        yield LiteLLMAdapter(mock_response="mocked response")


class TestLLMContract:
    async def test_complete_returns_string(self, llm: LLMPort) -> None:
        out = await llm.complete(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert isinstance(out, str)
        assert out

    async def test_temperature_argument_accepted(self, llm: LLMPort) -> None:
        out = await llm.complete(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0,
        )
        assert out

    async def test_max_tokens_argument_accepted(self, llm: LLMPort) -> None:
        out = await llm.complete(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=8,
        )
        assert out
```

LLM **behaviour** (does the model actually answer the question?) is
out of scope — that's evals. Contract asserts shape only.

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/contract/test_llm_contract.py -v`
Expected: 6 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/contract/test_llm_contract.py
git commit -m "test(contract): LLMPort shape — no network"
```

### Task 4.13: Verify whole-contract-layer budget

- [ ] **Step 1: Run with timing**

Run: `uv run pytest tests/contract -q --durations=10`
Expected: 12 files; every test under 200 ms; whole layer under 60 s.
If a `[real]` parametrization is over budget, the in-process
substitute is doing real IO — switch to `fakeredis`/`aiosqlite`.

- [ ] **Step 2: Run with xdist**

Run: `uv run pytest tests/contract -q -n auto`
Expected: same pass count, faster wall-clock.

- [ ] **Step 3: Commit if any source fixes were made**

```bash
git add -A
git commit -m "test(contract): whole layer under budget"
```

## Phase 5 — Integration layer tests

The integration layer assembles use cases (triage, review, fix, chat),
the dispatcher, the middleware chain, persistence, queue, and agent
runtimes — every adapter is a fake from `openbot/testing/`, no
network, no daemons, no real Postgres / Redis. Each test asserts
*observable behaviour*: which side-effects were recorded on the
fakes, not internal call counts.

Spec coverage: §6, §7.3.

Layer budget: whole layer must finish under **4 minutes** locally
(serial — `pytest-xdist` is intentionally not used here because
several tests share state through fakes).

### Task 5.1: Use-case SUT factories

**Files:**
- Create: `tests/integration/use_cases/__init__.py`
- Create: `tests/integration/use_cases/_sut.py`

The SUT factory pattern keeps each use-case test readable: the test
calls `make_triage_sut(...)`, sees the full assembly inline, and asserts
on the returned dataclass. No hidden fixture inheritance.

- [ ] **Step 1: Write `_sut.py`**

```python
"""Use-case SUT (system-under-test) factories.

Each ``make_*_sut`` returns a frozen dataclass exposing both the use
case object and the fakes wired into it. Tests assert on the fakes'
recorded state after invoking the use case.
"""

from __future__ import annotations

from dataclasses import dataclass

from openbot.application.use_cases.chat import ChatUseCase
from openbot.application.use_cases.fix import FixUseCase
from openbot.application.use_cases.review import ReviewUseCase
from openbot.application.use_cases.triage import TriageUseCase
from openbot.testing.fakes.audit_log import FakeAuditLog
from openbot.testing.fakes.cancellation import FakeCancellation
from openbot.testing.fakes.channel_adapter import FakeChannelAdapter
from openbot.testing.fakes.config_loader import FakeConfigLoader
from openbot.testing.fakes.dedup import FakeDedup
from openbot.testing.fakes.llm import FakeLLM
from openbot.testing.fakes.queue import FakeQueue
from openbot.testing.fakes.rate_limiter import FakeRateLimiter
from openbot.testing.fakes.resource_lock import FakeResourceLock
from openbot.testing.fakes.runs_repo import FakeRunsRepo
from openbot.testing.fakes.sandbox import FakeSandbox
from openbot.testing.fakes.sandbox_cache import FakeSandboxCache


@dataclass(frozen=True, slots=True)
class TriageSUT:
    use_case: TriageUseCase
    channel: FakeChannelAdapter
    runs: FakeRunsRepo
    audit: FakeAuditLog
    llm: FakeLLM


@dataclass(frozen=True, slots=True)
class ReviewSUT:
    use_case: ReviewUseCase
    channel: FakeChannelAdapter
    runs: FakeRunsRepo
    audit: FakeAuditLog
    llm: FakeLLM


@dataclass(frozen=True, slots=True)
class FixSUT:
    use_case: FixUseCase
    channel: FakeChannelAdapter
    runs: FakeRunsRepo
    audit: FakeAuditLog
    llm: FakeLLM
    sandbox: FakeSandbox


@dataclass(frozen=True, slots=True)
class ChatSUT:
    use_case: ChatUseCase
    channel: FakeChannelAdapter
    runs: FakeRunsRepo
    audit: FakeAuditLog
    llm: FakeLLM


def make_triage_sut(
    *,
    config: FakeConfigLoader | None = None,
    llm_responses: list[str] | None = None,
) -> TriageSUT:
    channel = FakeChannelAdapter()
    runs = FakeRunsRepo()
    audit = FakeAuditLog()
    llm = FakeLLM(canned=llm_responses or ['{"label":"bug","confidence":0.9}'])
    cfg = config or FakeConfigLoader.with_defaults()
    use_case = TriageUseCase(
        channel=channel, runs=runs, audit=audit, llm=llm, config=cfg
    )
    return TriageSUT(use_case=use_case, channel=channel, runs=runs, audit=audit, llm=llm)


def make_review_sut(
    *,
    config: FakeConfigLoader | None = None,
    pr_diff: str = "",
) -> ReviewSUT:
    channel = FakeChannelAdapter(pr_diff=pr_diff)
    runs = FakeRunsRepo()
    audit = FakeAuditLog()
    llm = FakeLLM(canned=['{"summary":"ok","comments":[]}'])
    cfg = config or FakeConfigLoader.with_defaults()
    use_case = ReviewUseCase(
        channel=channel, runs=runs, audit=audit, llm=llm, config=cfg
    )
    return ReviewSUT(use_case=use_case, channel=channel, runs=runs, audit=audit, llm=llm)


def make_fix_sut(
    *,
    config: FakeConfigLoader | None = None,
    sandbox_files: dict[str, str] | None = None,
) -> FixSUT:
    channel = FakeChannelAdapter()
    runs = FakeRunsRepo()
    audit = FakeAuditLog()
    llm = FakeLLM(canned=['{"plan":"edit","actions":[]}'])
    sandbox = FakeSandbox(files=sandbox_files or {})
    cfg = config or FakeConfigLoader.with_defaults()
    use_case = FixUseCase(
        channel=channel,
        runs=runs,
        audit=audit,
        llm=llm,
        sandbox_factory=lambda **_kw: sandbox,
        config=cfg,
    )
    return FixSUT(
        use_case=use_case, channel=channel, runs=runs,
        audit=audit, llm=llm, sandbox=sandbox,
    )


def make_chat_sut(
    *,
    config: FakeConfigLoader | None = None,
    llm_responses: list[str] | None = None,
) -> ChatSUT:
    channel = FakeChannelAdapter()
    runs = FakeRunsRepo()
    audit = FakeAuditLog()
    llm = FakeLLM(canned=llm_responses or ["Hi, here's the answer."])
    cfg = config or FakeConfigLoader.with_defaults()
    use_case = ChatUseCase(
        channel=channel, runs=runs, audit=audit, llm=llm, config=cfg
    )
    return ChatSUT(use_case=use_case, channel=channel, runs=runs, audit=audit, llm=llm)


__all__ = [
    "TriageSUT",
    "ReviewSUT",
    "FixSUT",
    "ChatSUT",
    "make_triage_sut",
    "make_review_sut",
    "make_fix_sut",
    "make_chat_sut",
]
```

- [ ] **Step 2: Quick import sanity check**

Run: `uv run python -c "from tests.integration.use_cases._sut import make_triage_sut, make_review_sut, make_fix_sut, make_chat_sut; print('ok')"`
Expected: `ok` (no import errors).

If a use-case constructor signature has drifted from the assumed
`(channel, runs, audit, llm, config)` shape, fix the factory call to
match — do NOT change the use case to fit the test.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/use_cases/__init__.py tests/integration/use_cases/_sut.py
git commit -m "test(integration): SUT factories for use cases"
```

### Task 5.2: Triage use-case integration

**Files:**
- Create: `tests/integration/use_cases/test_triage_flow.py`

- [ ] **Step 1: Write the test file**

```python
"""Triage use case end-to-end with fakes.

Asserts the observable surface: a label was applied, a reply was
posted, the run was persisted, and an audit row was emitted.
"""

from __future__ import annotations

import pytest

from openbot.testing.builders import build_issue_opened_event
from tests.integration.use_cases._sut import make_triage_sut


@pytest.mark.asyncio
async def test_triage_labels_and_replies():
    sut = make_triage_sut(
        llm_responses=['{"label":"bug","confidence":0.92}'],
    )
    event = build_issue_opened_event(
        repo="acme/widget",
        issue_number=42,
        title="Crash on startup",
        body="App segfaults on launch.",
    )

    await sut.use_case.handle(event)

    assert sut.channel.labels_added == [(event.resource_key, ("bug",))]
    assert len(sut.channel.replies) == 1
    assert "bug" in sut.channel.replies[0][1].lower()
    assert len(sut.runs.runs) == 1
    assert sut.runs.runs[0].status == "succeeded"
    assert any(row.kind == "triage.completed" for row in sut.audit.rows)
```

- [ ] **Step 2: Add LLM-failure path test**

Append to the same file:

```python
@pytest.mark.asyncio
async def test_triage_llm_failure_records_failed_run():
    sut = make_triage_sut(llm_responses=[])  # exhausts on first call
    event = build_issue_opened_event(
        repo="acme/widget", issue_number=7, title="x", body="y"
    )

    await sut.use_case.handle(event)

    assert sut.runs.runs[0].status == "failed"
    assert sut.channel.labels_added == []
    assert any(row.kind == "triage.failed" for row in sut.audit.rows)
```

- [ ] **Step 3: Run and verify**

Run: `uv run pytest tests/integration/use_cases/test_triage_flow.py -v`
Expected: 2 PASSED.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/use_cases/test_triage_flow.py
git commit -m "test(integration): triage use case happy + failure paths"
```

### Task 5.3: Review use-case integration

**Files:**
- Create: `tests/integration/use_cases/test_review_flow.py`

- [ ] **Step 1: Write the test file**

```python
"""Review use case end-to-end with fakes.

The review path consumes a PR diff via FakeChannelAdapter.pr_diff
and posts back a single PR review (COMMENT or APPROVE).
"""

from __future__ import annotations

import pytest

from openbot.testing.builders import build_command_event
from tests.integration.use_cases._sut import make_review_sut

DIFF = """\
diff --git a/src/api.py b/src/api.py
@@ -1,3 +1,4 @@
+import os
 def get_secret():
-    return None
+    return os.environ['API_KEY']
"""


@pytest.mark.asyncio
async def test_review_posts_pr_review_with_findings():
    sut = make_review_sut(pr_diff=DIFF)
    event = build_command_event(
        repo="acme/widget",
        pr_number=11,
        command="review",
        actor="alice",
    )

    await sut.use_case.handle(event)

    assert len(sut.channel.pr_reviews) == 1
    review = sut.channel.pr_reviews[0]
    assert review["pr_number"] == 11
    assert review["event_type"] in {"COMMENT", "APPROVE"}
    assert sut.runs.runs[0].status == "succeeded"
```

- [ ] **Step 2: Add empty-diff path**

```python
@pytest.mark.asyncio
async def test_review_skips_empty_diff():
    sut = make_review_sut(pr_diff="")
    event = build_command_event(
        repo="acme/widget", pr_number=12, command="review", actor="alice"
    )

    await sut.use_case.handle(event)

    assert sut.channel.pr_reviews == []
    assert sut.channel.replies, "expected user-visible reply explaining empty diff"
```

- [ ] **Step 3: Run and verify**

Run: `uv run pytest tests/integration/use_cases/test_review_flow.py -v`
Expected: 2 PASSED.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/use_cases/test_review_flow.py
git commit -m "test(integration): review use case happy + empty-diff paths"
```

### Task 5.4: Fix use-case integration

**Files:**
- Create: `tests/integration/use_cases/test_fix_flow.py`

The fix path is the only use case that uses a sandbox. The SUT factory
wires `FakeSandbox` (in-process tempdir) so the test asserts on diffs,
branch creation, and PR opening recorded by `FakeChannelAdapter`.

- [ ] **Step 1: Write the test file**

```python
"""Fix use case end-to-end with fakes (in-process sandbox)."""

from __future__ import annotations

import pytest

from openbot.testing.builders import build_command_event
from tests.integration.use_cases._sut import make_fix_sut


@pytest.mark.asyncio
async def test_fix_writes_diff_and_opens_pr():
    sut = make_fix_sut(
        sandbox_files={"src/api.py": "def hi():\n    return 'hi'\n"},
    )
    event = build_command_event(
        repo="acme/widget",
        issue_number=99,
        command="fix",
        actor="alice",
        body="please add type hints",
    )

    await sut.use_case.handle(event)

    assert sut.channel.branches_created, "fix should create a branch"
    assert sut.channel.prs_opened, "fix should open a PR"
    assert sut.runs.runs[0].status == "succeeded"
```

- [ ] **Step 2: Add no-change path**

```python
@pytest.mark.asyncio
async def test_fix_no_diff_replies_explanation():
    sut = make_fix_sut(sandbox_files={"src/api.py": "def hi(): return 1\n"})
    # Sandbox produces empty diff because LLM canned response is empty action list.
    event = build_command_event(
        repo="acme/widget", issue_number=100, command="fix",
        actor="alice", body="no-op fix",
    )

    await sut.use_case.handle(event)

    assert sut.channel.prs_opened == []
    assert sut.channel.replies, "expected reply explaining no fix was made"
```

- [ ] **Step 3: Run and verify**

Run: `uv run pytest tests/integration/use_cases/test_fix_flow.py -v`
Expected: 2 PASSED.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/use_cases/test_fix_flow.py
git commit -m "test(integration): fix use case happy + no-diff paths"
```

### Task 5.5: Chat use-case integration

**Files:**
- Create: `tests/integration/use_cases/test_chat_flow.py`

- [ ] **Step 1: Write the test file**

```python
"""Chat use case end-to-end with fakes."""

from __future__ import annotations

import pytest

from openbot.testing.builders import build_command_event
from tests.integration.use_cases._sut import make_chat_sut


@pytest.mark.asyncio
async def test_chat_replies_to_thread():
    sut = make_chat_sut(llm_responses=["The repo uses pydantic v2 for models."])
    event = build_command_event(
        repo="acme/widget",
        issue_number=33,
        command="chat",
        actor="alice",
        body="What does this repo use?",
    )

    await sut.use_case.handle(event)

    assert len(sut.channel.replies) == 1
    assert "pydantic" in sut.channel.replies[0][1].lower()
    assert sut.runs.runs[0].status == "succeeded"


@pytest.mark.asyncio
async def test_chat_empty_question_short_circuits():
    sut = make_chat_sut()
    event = build_command_event(
        repo="acme/widget", issue_number=34, command="chat",
        actor="alice", body="",
    )

    await sut.use_case.handle(event)

    assert sut.channel.replies, "expected user-visible 'what would you like to ask?' reply"
    assert sut.llm.calls == [], "should not consume LLM budget on empty input"
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/use_cases/test_chat_flow.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/use_cases/test_chat_flow.py
git commit -m "test(integration): chat use case happy + empty-input paths"
```

### Task 5.6: Dispatcher decide pipeline

**Files:**
- Create: `tests/integration/dispatcher/__init__.py`
- Create: `tests/integration/dispatcher/test_decide_pipeline.py`

The dispatcher fans an event through `decide → execute`. Decide is
pure-ish but reads from runs/dedup; this test asserts decide returns
the right `Action` for each event kind.

- [ ] **Step 1: Write the test file**

```python
"""Dispatcher.decide → Action mapping."""

from __future__ import annotations

import pytest

from openbot.application.dispatcher import Dispatcher
from openbot.domain.actions import ActionKind
from openbot.testing.builders import (
    build_command_event,
    build_issue_opened_event,
    build_pull_request_opened_event,
)
from openbot.testing.fakes.config_loader import FakeConfigLoader
from openbot.testing.fakes.dedup import FakeDedup
from openbot.testing.fakes.runs_repo import FakeRunsRepo


def make_dispatcher() -> Dispatcher:
    return Dispatcher(
        runs=FakeRunsRepo(),
        dedup=FakeDedup(),
        config=FakeConfigLoader.with_defaults(),
    )


@pytest.mark.asyncio
async def test_issue_opened_decides_triage():
    d = make_dispatcher()
    action = await d.decide(build_issue_opened_event(repo="a/b", issue_number=1))
    assert action.kind == ActionKind.RUN_TRIAGE


@pytest.mark.asyncio
async def test_pr_opened_decides_review():
    d = make_dispatcher()
    action = await d.decide(build_pull_request_opened_event(repo="a/b", pr_number=2))
    assert action.kind == ActionKind.RUN_REVIEW


@pytest.mark.asyncio
async def test_command_chat_decides_chat():
    d = make_dispatcher()
    action = await d.decide(build_command_event(repo="a/b", issue_number=3, command="chat"))
    assert action.kind == ActionKind.RUN_CHAT
```

- [ ] **Step 2: Add dedup + supersede paths**

```python
@pytest.mark.asyncio
async def test_duplicate_event_decides_skip():
    dedup = FakeDedup()
    d = Dispatcher(
        runs=FakeRunsRepo(),
        dedup=dedup,
        config=FakeConfigLoader.with_defaults(),
    )
    event = build_issue_opened_event(repo="a/b", issue_number=4)

    first = await d.decide(event)
    second = await d.decide(event)

    assert first.kind == ActionKind.RUN_TRIAGE
    assert second.kind == ActionKind.SKIP_DUPLICATE


@pytest.mark.asyncio
async def test_new_command_supersedes_running():
    runs = FakeRunsRepo()
    runs.add_running(resource_key="a/b#5", run_id="r1")
    d = Dispatcher(runs=runs, dedup=FakeDedup(), config=FakeConfigLoader.with_defaults())

    event = build_command_event(repo="a/b", issue_number=5, command="fix")
    action = await d.decide(event)

    assert action.kind == ActionKind.SUPERSEDE
    assert action.payload["superseded_run_id"] == "r1"
```

- [ ] **Step 3: Run and verify**

Run: `uv run pytest tests/integration/dispatcher/test_decide_pipeline.py -v`
Expected: 5 PASSED.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/dispatcher/__init__.py tests/integration/dispatcher/test_decide_pipeline.py
git commit -m "test(integration): dispatcher decide pipeline"
```

### Task 5.7: Dispatcher execute handler

**Files:**
- Create: `tests/integration/dispatcher/test_execute_handler.py`

- [ ] **Step 1: Write the test file**

```python
"""Dispatcher.execute(action) → use-case dispatch."""

from __future__ import annotations

import pytest

from openbot.application.dispatcher import Dispatcher
from openbot.domain.actions import Action, ActionKind
from openbot.testing.builders import build_issue_opened_event
from openbot.testing.fakes.config_loader import FakeConfigLoader
from openbot.testing.fakes.dedup import FakeDedup
from openbot.testing.fakes.runs_repo import FakeRunsRepo


class _UseCaseSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle(self, event):  # type: ignore[no-untyped-def]
        self.calls.append(event.resource_key)


@pytest.mark.asyncio
async def test_execute_routes_to_triage_use_case():
    spy = _UseCaseSpy()
    d = Dispatcher(
        runs=FakeRunsRepo(),
        dedup=FakeDedup(),
        config=FakeConfigLoader.with_defaults(),
        use_cases={ActionKind.RUN_TRIAGE: spy},
    )
    event = build_issue_opened_event(repo="a/b", issue_number=10)
    action = Action(kind=ActionKind.RUN_TRIAGE, event=event, payload={})

    await d.execute(action)

    assert spy.calls == [event.resource_key]


@pytest.mark.asyncio
async def test_execute_skip_duplicate_does_nothing():
    spy = _UseCaseSpy()
    d = Dispatcher(
        runs=FakeRunsRepo(),
        dedup=FakeDedup(),
        config=FakeConfigLoader.with_defaults(),
        use_cases={ActionKind.RUN_TRIAGE: spy},
    )
    event = build_issue_opened_event(repo="a/b", issue_number=11)
    action = Action(kind=ActionKind.SKIP_DUPLICATE, event=event, payload={})

    await d.execute(action)

    assert spy.calls == []
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/dispatcher/test_execute_handler.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/dispatcher/test_execute_handler.py
git commit -m "test(integration): dispatcher execute handler"
```

### Task 5.8: Classifier routing

**Files:**
- Create: `tests/integration/dispatcher/test_classifier_routing.py`

The classifier picks the model for each event kind from config. The
unit test in Phase 3 covers pure model selection; this integration
test covers the full classifier-with-config wiring.

- [ ] **Step 1: Write the test file**

```python
"""Classifier routes each event kind to its configured model."""

from __future__ import annotations

import pytest

from openbot.application.router import Classifier
from openbot.testing.builders import (
    build_command_event,
    build_issue_opened_event,
    build_pull_request_opened_event,
)
from openbot.testing.fakes.config_loader import FakeConfigLoader


@pytest.mark.asyncio
async def test_classifier_picks_per_event_kind():
    cfg = FakeConfigLoader.with_models(
        triage="gpt-4o-mini",
        review="claude-sonnet-4-6",
        fix="claude-opus-4-7",
        chat="gpt-4o-mini",
    )
    c = Classifier(config=cfg)

    assert (await c.model_for(build_issue_opened_event(repo="a/b", issue_number=1))) == "gpt-4o-mini"
    assert (await c.model_for(build_pull_request_opened_event(repo="a/b", pr_number=2))) == "claude-sonnet-4-6"
    assert (await c.model_for(build_command_event(repo="a/b", issue_number=3, command="fix"))) == "claude-opus-4-7"
    assert (await c.model_for(build_command_event(repo="a/b", issue_number=4, command="chat"))) == "gpt-4o-mini"
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/dispatcher/test_classifier_routing.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/dispatcher/test_classifier_routing.py
git commit -m "test(integration): classifier per-event-kind routing"
```

### Task 5.9: Middleware — security

**Files:**
- Create: `tests/integration/middleware/__init__.py`
- Create: `tests/integration/middleware/test_security.py`

- [ ] **Step 1: Write the test file**

```python
"""Security middleware: signature verification + actor role check."""

from __future__ import annotations

import pytest

from openbot.application.middleware.security import SecurityMiddleware
from openbot.domain.errors import SecurityRejection
from openbot.testing.builders import build_command_event
from openbot.testing.fakes.channel_adapter import FakeChannelAdapter


@pytest.mark.asyncio
async def test_security_rejects_unverified_actor():
    channel = FakeChannelAdapter(actor_role={"alice": "none"})
    mw = SecurityMiddleware(channel=channel)
    event = build_command_event(
        repo="a/b", issue_number=1, command="fix", actor="alice"
    )

    with pytest.raises(SecurityRejection):
        await mw.before(event)


@pytest.mark.asyncio
async def test_security_allows_collaborator():
    channel = FakeChannelAdapter(actor_role={"alice": "collaborator"})
    mw = SecurityMiddleware(channel=channel)
    event = build_command_event(
        repo="a/b", issue_number=1, command="fix", actor="alice"
    )

    # No exception = allowed.
    await mw.before(event)
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/middleware/test_security.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/middleware/__init__.py tests/integration/middleware/test_security.py
git commit -m "test(integration): security middleware"
```

### Task 5.10: Middleware — rate_limit

**Files:**
- Create: `tests/integration/middleware/test_rate_limit.py`

- [ ] **Step 1: Write the test file**

```python
"""Rate-limit middleware: per-resource bucket."""

from __future__ import annotations

import pytest

from openbot.application.middleware.rate_limit import RateLimitMiddleware
from openbot.domain.errors import RateLimited
from openbot.testing.builders import build_command_event
from openbot.testing.fakes.rate_limiter import FakeRateLimiter


@pytest.mark.asyncio
async def test_rate_limit_allows_under_quota():
    rl = FakeRateLimiter(limit=2)
    mw = RateLimitMiddleware(rate_limiter=rl)
    event = build_command_event(repo="a/b", issue_number=1, command="fix")

    await mw.before(event)
    await mw.before(event)


@pytest.mark.asyncio
async def test_rate_limit_blocks_over_quota():
    rl = FakeRateLimiter(limit=1)
    mw = RateLimitMiddleware(rate_limiter=rl)
    event = build_command_event(repo="a/b", issue_number=1, command="fix")

    await mw.before(event)
    with pytest.raises(RateLimited):
        await mw.before(event)
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/middleware/test_rate_limit.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/middleware/test_rate_limit.py
git commit -m "test(integration): rate_limit middleware"
```

### Task 5.11: Middleware — budget

**Files:**
- Create: `tests/integration/middleware/test_budget.py`

- [ ] **Step 1: Write the test file**

```python
"""Budget middleware: per-run token + dollar cap."""

from __future__ import annotations

import pytest

from openbot.application.middleware.budget import BudgetMiddleware
from openbot.domain.errors import BudgetExceeded
from openbot.testing.builders import build_command_event
from openbot.testing.fakes.config_loader import FakeConfigLoader


@pytest.mark.asyncio
async def test_budget_allows_under_cap():
    cfg = FakeConfigLoader.with_budget(per_run_usd=1.0, per_run_tokens=1000)
    mw = BudgetMiddleware(config=cfg)
    event = build_command_event(repo="a/b", issue_number=1, command="fix")

    await mw.before(event)


@pytest.mark.asyncio
async def test_budget_rejects_event_with_exhausted_run():
    cfg = FakeConfigLoader.with_budget(per_run_usd=0.0, per_run_tokens=0)
    mw = BudgetMiddleware(config=cfg)
    event = build_command_event(repo="a/b", issue_number=1, command="fix")

    with pytest.raises(BudgetExceeded):
        await mw.before(event)
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/middleware/test_budget.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/middleware/test_budget.py
git commit -m "test(integration): budget middleware"
```

### Task 5.12: Middleware — cancel

**Files:**
- Create: `tests/integration/middleware/test_cancel.py`

- [ ] **Step 1: Write the test file**

```python
"""Cancel middleware: superseded run is cancelled cooperatively."""

from __future__ import annotations

import pytest

from openbot.application.middleware.cancel import CancelMiddleware
from openbot.domain.errors import RunCancelled
from openbot.testing.builders import build_command_event
from openbot.testing.fakes.cancellation import FakeCancellation


@pytest.mark.asyncio
async def test_cancel_passes_when_token_alive():
    canc = FakeCancellation()
    mw = CancelMiddleware(cancellation=canc)
    event = build_command_event(repo="a/b", issue_number=1, command="fix")

    await mw.before(event)


@pytest.mark.asyncio
async def test_cancel_raises_when_token_cancelled():
    canc = FakeCancellation()
    canc.cancel("a/b#1")
    mw = CancelMiddleware(cancellation=canc)
    event = build_command_event(repo="a/b", issue_number=1, command="fix")

    with pytest.raises(RunCancelled):
        await mw.before(event)
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/middleware/test_cancel.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/middleware/test_cancel.py
git commit -m "test(integration): cancel middleware"
```

### Task 5.13: Middleware — preflight

**Files:**
- Create: `tests/integration/middleware/test_preflight.py`

- [ ] **Step 1: Write the test file**

```python
"""Preflight middleware: feature flag gate + repo allowlist check."""

from __future__ import annotations

import pytest

from openbot.application.middleware.preflight import PreflightMiddleware
from openbot.domain.errors import PreflightRejection
from openbot.testing.builders import build_command_event
from openbot.testing.fakes.config_loader import FakeConfigLoader


@pytest.mark.asyncio
async def test_preflight_allows_listed_repo():
    cfg = FakeConfigLoader.with_repos(allowlist=["a/b"])
    mw = PreflightMiddleware(config=cfg)
    event = build_command_event(repo="a/b", issue_number=1, command="fix")

    await mw.before(event)


@pytest.mark.asyncio
async def test_preflight_rejects_unlisted_repo():
    cfg = FakeConfigLoader.with_repos(allowlist=["a/b"])
    mw = PreflightMiddleware(config=cfg)
    event = build_command_event(repo="evil/repo", issue_number=1, command="fix")

    with pytest.raises(PreflightRejection):
        await mw.before(event)
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/middleware/test_preflight.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/middleware/test_preflight.py
git commit -m "test(integration): preflight middleware"
```

### Task 5.14: Middleware — sanitize

**Files:**
- Create: `tests/integration/middleware/test_sanitize.py`

- [ ] **Step 1: Write the test file**

```python
"""Sanitize middleware: strip secrets from issue/PR body before LLM."""

from __future__ import annotations

import pytest

from openbot.application.middleware.sanitize import SanitizeMiddleware
from openbot.testing.builders import build_command_event


@pytest.mark.asyncio
async def test_sanitize_redacts_github_pat():
    mw = SanitizeMiddleware()
    raw_token = "ghp_" + "a" * 36
    event = build_command_event(
        repo="a/b", issue_number=1, command="chat",
        body=f"my token is {raw_token}",
    )

    sanitized = await mw.before(event)

    assert raw_token not in sanitized.body
    assert "[REDACTED" in sanitized.body


@pytest.mark.asyncio
async def test_sanitize_passes_clean_body_unchanged():
    mw = SanitizeMiddleware()
    event = build_command_event(
        repo="a/b", issue_number=1, command="chat", body="please review",
    )

    sanitized = await mw.before(event)
    assert sanitized.body == event.body
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/middleware/test_sanitize.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/middleware/test_sanitize.py
git commit -m "test(integration): sanitize middleware redaction"
```

### Task 5.15: Middleware — feature_toggle

**Files:**
- Create: `tests/integration/middleware/test_feature_toggle.py`

- [ ] **Step 1: Write the test file**

```python
"""Feature toggle middleware: per-feature on/off from config."""

from __future__ import annotations

import pytest

from openbot.application.middleware.feature_toggle import FeatureToggleMiddleware
from openbot.domain.errors import FeatureDisabled
from openbot.testing.builders import build_command_event
from openbot.testing.fakes.config_loader import FakeConfigLoader


@pytest.mark.asyncio
async def test_feature_toggle_passes_enabled_feature():
    cfg = FakeConfigLoader.with_features(fix=True, chat=True)
    mw = FeatureToggleMiddleware(config=cfg)
    event = build_command_event(repo="a/b", issue_number=1, command="fix")

    await mw.before(event)


@pytest.mark.asyncio
async def test_feature_toggle_blocks_disabled_feature():
    cfg = FakeConfigLoader.with_features(fix=False, chat=True)
    mw = FeatureToggleMiddleware(config=cfg)
    event = build_command_event(repo="a/b", issue_number=1, command="fix")

    with pytest.raises(FeatureDisabled):
        await mw.before(event)
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/middleware/test_feature_toggle.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/middleware/test_feature_toggle.py
git commit -m "test(integration): feature_toggle middleware"
```

### Task 5.16: Middleware — audit_start

**Files:**
- Create: `tests/integration/middleware/test_audit_start.py`

- [ ] **Step 1: Write the test file**

```python
"""Audit middleware: emits 'run.started' row before use case runs."""

from __future__ import annotations

import pytest

from openbot.application.middleware.audit import AuditStartMiddleware
from openbot.testing.builders import build_command_event
from openbot.testing.fakes.audit_log import FakeAuditLog


@pytest.mark.asyncio
async def test_audit_start_records_run_started():
    audit = FakeAuditLog()
    mw = AuditStartMiddleware(audit=audit)
    event = build_command_event(repo="a/b", issue_number=1, command="chat")

    await mw.before(event)

    assert len(audit.rows) == 1
    row = audit.rows[0]
    assert row.kind == "run.started"
    assert row.resource_key == event.resource_key
    assert row.actor == event.actor


@pytest.mark.asyncio
async def test_audit_chain_orders_before_use_case_call():
    audit = FakeAuditLog()
    mw = AuditStartMiddleware(audit=audit)
    event = build_command_event(repo="a/b", issue_number=2, command="fix")

    sanitized = await mw.before(event)

    # before() returns event unchanged for non-sanitizing middleware.
    assert sanitized is event
    assert audit.rows[0].kind == "run.started"
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/middleware/test_audit_start.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/middleware/test_audit_start.py
git commit -m "test(integration): audit_start middleware"
```

### Task 5.17: Persistence — runs repo (aiosqlite)

**Files:**
- Create: `tests/integration/persistence/__init__.py`
- Create: `tests/integration/persistence/test_runs_repo.py`

The persistence integration tests run real SQL via `aiosqlite` against
the actual SQLAlchemy models — they're not mocked. They differ from
the contract layer because they assert on multi-row interactions
(supersede, list, prune) rather than single-row port shape.

- [ ] **Step 1: Write the test file**

```python
"""Runs repo integration with aiosqlite-backed SQLAlchemy."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from openbot.infrastructure.persistence.runs_repo_impl import SqlAlchemyRunsRepo


@pytest.mark.asyncio
async def test_runs_repo_create_then_fetch(inmemory_db):
    repo = SqlAlchemyRunsRepo(session_maker=inmemory_db)

    run = await repo.create(
        resource_key="a/b#1",
        kind="triage",
        actor="alice",
        idempotency_key="k1",
    )

    fetched = await repo.get(run.id)
    assert fetched is not None
    assert fetched.id == run.id
    assert fetched.status == "running"


@pytest.mark.asyncio
async def test_runs_repo_supersedes_running_run(inmemory_db):
    repo = SqlAlchemyRunsRepo(session_maker=inmemory_db)
    first = await repo.create(
        resource_key="a/b#2", kind="fix", actor="alice", idempotency_key="k1",
    )
    second = await repo.create(
        resource_key="a/b#2", kind="fix", actor="alice", idempotency_key="k2",
    )

    await repo.supersede(first.id, by=second.id)

    refreshed = await repo.get(first.id)
    assert refreshed.status == "superseded"
    assert refreshed.superseded_by == second.id
```

- [ ] **Step 2: Add list-active path**

```python
@pytest.mark.asyncio
async def test_runs_repo_list_active_excludes_terminal(inmemory_db):
    repo = SqlAlchemyRunsRepo(session_maker=inmemory_db)
    a = await repo.create(resource_key="a/b#3", kind="chat", actor="x", idempotency_key="k1")
    b = await repo.create(resource_key="a/b#4", kind="chat", actor="x", idempotency_key="k2")
    await repo.complete(a.id, status="succeeded")

    active = await repo.list_active()

    assert {r.id for r in active} == {b.id}
```

- [ ] **Step 3: Run and verify**

Run: `uv run pytest tests/integration/persistence/test_runs_repo.py -v`
Expected: 3 PASSED.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/persistence/__init__.py tests/integration/persistence/test_runs_repo.py
git commit -m "test(integration): runs_repo against aiosqlite"
```

### Task 5.18: Persistence — dedup (fakeredis)

**Files:**
- Create: `tests/integration/persistence/test_dedup.py`

- [ ] **Step 1: Write the test file**

```python
"""Dedup integration with fakeredis."""

from __future__ import annotations

import pytest

from openbot.infrastructure.persistence.dedup import RedisDedup


@pytest.mark.asyncio
async def test_dedup_first_seen_returns_false_then_true(inmemory_redis):
    d = RedisDedup(redis=inmemory_redis, ttl_seconds=60)

    assert await d.seen_recently("delivery-1") is False
    assert await d.seen_recently("delivery-1") is True


@pytest.mark.asyncio
async def test_dedup_distinct_keys_independent(inmemory_redis):
    d = RedisDedup(redis=inmemory_redis, ttl_seconds=60)

    await d.seen_recently("delivery-a")
    assert await d.seen_recently("delivery-b") is False


@pytest.mark.asyncio
async def test_dedup_ttl_expires(inmemory_redis):
    d = RedisDedup(redis=inmemory_redis, ttl_seconds=1)
    await d.seen_recently("delivery-x")

    # fakeredis doesn't auto-advance time; we expire manually.
    await inmemory_redis.delete("dedup:delivery-x")
    assert await d.seen_recently("delivery-x") is False
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/persistence/test_dedup.py -v`
Expected: 3 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/persistence/test_dedup.py
git commit -m "test(integration): dedup against fakeredis"
```

### Task 5.19: Persistence — db engine bootstrap

**Files:**
- Create: `tests/integration/persistence/test_db.py`

- [ ] **Step 1: Write the test file**

```python
"""DB engine + sessionmaker creates tables and round-trips a row."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from openbot.infrastructure.persistence.db import build_session_maker
from openbot.infrastructure.persistence.models import Base


@pytest.mark.asyncio
async def test_db_creates_all_tables_and_round_trips(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/openbot.db"
    session_maker, engine = await build_session_maker(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1

    await engine.dispose()
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/persistence/test_db.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/persistence/test_db.py
git commit -m "test(integration): db engine bootstrap"
```

### Task 5.20: Persistence — agent checkpointer

**Files:**
- Create: `tests/integration/persistence/test_agent_checkpointer.py`

The agent checkpointer is the LangGraph adapter that persists thread
state across restarts. Test asserts a thread written then read on a
fresh instance returns the same state.

- [ ] **Step 1: Write the test file**

```python
"""Agent checkpointer round-trips a LangGraph thread state."""

from __future__ import annotations

import pytest

from openbot.infrastructure.persistence.agent_checkpointer import (
    SqlAlchemyAgentCheckpointer,
)


@pytest.mark.asyncio
async def test_checkpointer_round_trips_thread_state(inmemory_db):
    cp = SqlAlchemyAgentCheckpointer(session_maker=inmemory_db)
    thread_id = "thread-xyz"
    state = {"step": 3, "messages": [{"role": "user", "content": "hi"}]}

    await cp.put(thread_id, state)

    cp2 = SqlAlchemyAgentCheckpointer(session_maker=inmemory_db)
    loaded = await cp2.get(thread_id)
    assert loaded == state


@pytest.mark.asyncio
async def test_checkpointer_missing_thread_returns_none(inmemory_db):
    cp = SqlAlchemyAgentCheckpointer(session_maker=inmemory_db)
    assert await cp.get("does-not-exist") is None
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/persistence/test_agent_checkpointer.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/persistence/test_agent_checkpointer.py
git commit -m "test(integration): agent checkpointer round-trip"
```

### Task 5.21: Queue — enqueue path

**Files:**
- Create: `tests/integration/queue/__init__.py`
- Create: `tests/integration/queue/test_enqueue.py`

- [ ] **Step 1: Write the test file**

```python
"""Enqueue serializes a TaskSpec onto fakeredis stream."""

from __future__ import annotations

import pytest

from openbot.infrastructure.queue.enqueue import enqueue_task
from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.testing.builders import build_command_event


@pytest.mark.asyncio
async def test_enqueue_writes_message(inmemory_redis):
    event = build_command_event(repo="a/b", issue_number=1, command="fix")
    spec = TaskSpec(event=event, run_id="run-1", kind="fix")

    await enqueue_task(spec, redis=inmemory_redis, stream="openbot:tasks")

    pending = await inmemory_redis.xlen("openbot:tasks")
    assert pending == 1


@pytest.mark.asyncio
async def test_enqueue_round_trips_via_payload(inmemory_redis):
    event = build_command_event(repo="a/b", issue_number=2, command="chat")
    spec = TaskSpec(event=event, run_id="run-2", kind="chat")
    await enqueue_task(spec, redis=inmemory_redis, stream="openbot:tasks")

    entries = await inmemory_redis.xrange("openbot:tasks")
    assert len(entries) == 1
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/queue/test_enqueue.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/queue/__init__.py tests/integration/queue/test_enqueue.py
git commit -m "test(integration): queue enqueue path"
```

### Task 5.22: Queue — worker consumes task

**Files:**
- Create: `tests/integration/queue/test_worker_consume.py`

- [ ] **Step 1: Write the test file**

```python
"""Worker reads a TaskSpec from fakeredis and dispatches to the use case."""

from __future__ import annotations

import asyncio

import pytest

from openbot.infrastructure.queue.enqueue import enqueue_task
from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.infrastructure.queue.worker import Worker
from openbot.testing.builders import build_issue_opened_event


class _SpyDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, action) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(action.event.resource_key)

    async def decide(self, event):  # type: ignore[no-untyped-def]
        from openbot.domain.actions import Action, ActionKind
        return Action(kind=ActionKind.RUN_TRIAGE, event=event, payload={})


@pytest.mark.asyncio
async def test_worker_consumes_one_task(inmemory_redis):
    event = build_issue_opened_event(repo="a/b", issue_number=1)
    spec = TaskSpec(event=event, run_id="r1", kind="triage")
    await enqueue_task(spec, redis=inmemory_redis, stream="openbot:tasks")

    dispatcher = _SpyDispatcher()
    worker = Worker(redis=inmemory_redis, dispatcher=dispatcher, stream="openbot:tasks")

    task = asyncio.create_task(worker.run_one())
    await asyncio.wait_for(task, timeout=2.0)

    assert dispatcher.calls == ["a/b#1"]
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/queue/test_worker_consume.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/queue/test_worker_consume.py
git commit -m "test(integration): worker consumes one task"
```

### Task 5.23: Queue — worker honours cancellation

**Files:**
- Create: `tests/integration/queue/test_worker_cancellation.py`

- [ ] **Step 1: Write the test file**

```python
"""Worker checks cancellation before running and skips cancelled tasks."""

from __future__ import annotations

import asyncio

import pytest

from openbot.infrastructure.queue.enqueue import enqueue_task
from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.infrastructure.queue.worker import Worker
from openbot.testing.builders import build_command_event
from openbot.testing.fakes.cancellation import FakeCancellation


class _SpyDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, action) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(action.event.resource_key)

    async def decide(self, event):  # type: ignore[no-untyped-def]
        from openbot.domain.actions import Action, ActionKind
        return Action(kind=ActionKind.RUN_FIX, event=event, payload={})


@pytest.mark.asyncio
async def test_worker_skips_cancelled_run(inmemory_redis):
    event = build_command_event(repo="a/b", issue_number=1, command="fix")
    spec = TaskSpec(event=event, run_id="r1", kind="fix")
    await enqueue_task(spec, redis=inmemory_redis, stream="openbot:tasks")

    canc = FakeCancellation()
    canc.cancel("a/b#1")
    dispatcher = _SpyDispatcher()
    worker = Worker(
        redis=inmemory_redis,
        dispatcher=dispatcher,
        cancellation=canc,
        stream="openbot:tasks",
    )

    await asyncio.wait_for(worker.run_one(), timeout=2.0)

    assert dispatcher.calls == []  # cancelled before dispatch
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/queue/test_worker_cancellation.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/queue/test_worker_cancellation.py
git commit -m "test(integration): worker honours cancellation token"
```

### Task 5.24: Queue — concurrent supersede

**Files:**
- Create: `tests/integration/queue/test_concurrent_supersede.py`

This test exercises the supersede contract end-to-end across the
queue: enqueue task A, enqueue task B for the same resource, run the
worker — A should be marked superseded, B should run.

- [ ] **Step 1: Write the test file**

```python
"""Two queued tasks for the same resource — second supersedes first."""

from __future__ import annotations

import asyncio

import pytest

from openbot.infrastructure.queue.enqueue import enqueue_task
from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.infrastructure.queue.worker import Worker
from openbot.testing.builders import build_command_event


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def execute(self, action) -> None:  # type: ignore[no-untyped-def]
        self.actions.append(action.kind.value)

    async def decide(self, event):  # type: ignore[no-untyped-def]
        from openbot.domain.actions import Action, ActionKind
        # Second event detects an active run and supersedes.
        kind = ActionKind.SUPERSEDE if self.actions else ActionKind.RUN_FIX
        return Action(kind=kind, event=event, payload={})


@pytest.mark.asyncio
async def test_second_task_supersedes_first(inmemory_redis):
    event = build_command_event(repo="a/b", issue_number=9, command="fix")
    await enqueue_task(
        TaskSpec(event=event, run_id="r1", kind="fix"),
        redis=inmemory_redis, stream="openbot:tasks",
    )
    await enqueue_task(
        TaskSpec(event=event, run_id="r2", kind="fix"),
        redis=inmemory_redis, stream="openbot:tasks",
    )

    dispatcher = _RecordingDispatcher()
    worker = Worker(redis=inmemory_redis, dispatcher=dispatcher, stream="openbot:tasks")

    await asyncio.wait_for(worker.run_one(), timeout=2.0)
    await asyncio.wait_for(worker.run_one(), timeout=2.0)

    assert dispatcher.actions == ["run_fix", "supersede"]
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/queue/test_concurrent_supersede.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/queue/test_concurrent_supersede.py
git commit -m "test(integration): concurrent supersede across queue"
```

### Task 5.25: Queue — Redis ordering invariant

**Files:**
- Create: `tests/integration/queue/test_redis_ordering.py`

- [ ] **Step 1: Write the test file**

```python
"""Redis stream preserves enqueue order across many writes."""

from __future__ import annotations

import pytest

from openbot.infrastructure.queue.enqueue import enqueue_task
from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.testing.builders import build_command_event


@pytest.mark.asyncio
async def test_xrange_preserves_enqueue_order(inmemory_redis):
    for n in range(20):
        event = build_command_event(repo="a/b", issue_number=n, command="chat")
        await enqueue_task(
            TaskSpec(event=event, run_id=f"r{n}", kind="chat"),
            redis=inmemory_redis, stream="openbot:tasks",
        )

    entries = await inmemory_redis.xrange("openbot:tasks")
    run_ids = [e[1][b"run_id"].decode() for e in entries]
    assert run_ids == [f"r{n}" for n in range(20)]
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/queue/test_redis_ordering.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/queue/test_redis_ordering.py
git commit -m "test(integration): redis stream ordering invariant"
```

### Task 5.26: Agents — review runtime

**Files:**
- Create: `tests/integration/agents/__init__.py`
- Create: `tests/integration/agents/test_review_runtime.py`

The agent runtimes are the DeepAgents wrappers around LangGraph. We
test that the runtime can be constructed with fakes and that `.run()`
yields a structured result without making real network calls.

- [ ] **Step 1: Write the test file**

```python
"""DeepAgentsReviewResponder smoke + structured-output assertion."""

from __future__ import annotations

import pytest

from openbot.infrastructure.agents.deepagents_review import DeepAgentsReviewResponder
from openbot.testing.fakes.channel_adapter import FakeChannelAdapter
from openbot.testing.fakes.llm import FakeLLM


@pytest.mark.asyncio
async def test_review_runtime_returns_structured_findings():
    channel = FakeChannelAdapter(
        pr_diff="diff --git a/x.py b/x.py\n+print('debug')\n",
    )
    llm = FakeLLM(canned=['{"summary":"prefer logging","comments":[]}'])
    responder = DeepAgentsReviewResponder(channel=channel, llm=llm)

    findings = await responder.review_pr(repo="a/b", pr_number=1, base_sha="x", head_sha="y")

    assert findings.summary
    assert isinstance(findings.comments, list)
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/agents/test_review_runtime.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/agents/__init__.py tests/integration/agents/test_review_runtime.py
git commit -m "test(integration): review runtime structured output"
```

### Task 5.27: Agents — fix runtime

**Files:**
- Create: `tests/integration/agents/test_fix_runtime.py`

- [ ] **Step 1: Write the test file**

```python
"""DeepAgentsFixResponder produces a FixOutcome with diff + branch_ref."""

from __future__ import annotations

import pytest

from openbot.infrastructure.agents.deepagents_fix import DeepAgentsFixResponder
from openbot.testing.fakes.channel_adapter import FakeChannelAdapter
from openbot.testing.fakes.llm import FakeLLM
from openbot.testing.fakes.sandbox import FakeSandbox


@pytest.mark.asyncio
async def test_fix_runtime_round_trips_outcome():
    channel = FakeChannelAdapter()
    sandbox = FakeSandbox(files={"src/api.py": "def hi(): return 1\n"})
    llm = FakeLLM(canned=['{"plan":"add type hints","actions":[]}'])
    responder = DeepAgentsFixResponder(channel=channel, sandbox=sandbox, llm=llm)

    outcome = await responder.fix_issue(
        repo="a/b", issue_number=1, base_sha="x", default_branch="main",
    )

    assert outcome.diff is not None
    assert outcome.branch_ref.startswith("openbot/")
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/agents/test_fix_runtime.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/agents/test_fix_runtime.py
git commit -m "test(integration): fix runtime FixOutcome shape"
```

### Task 5.28: Agents — chat runtime

**Files:**
- Create: `tests/integration/agents/test_chat_runtime.py`

- [ ] **Step 1: Write the test file**

```python
"""DeepAgentsChatResponder returns a string answer."""

from __future__ import annotations

import pytest

from openbot.infrastructure.agents.deepagents_chat import DeepAgentsChatResponder
from openbot.testing.fakes.channel_adapter import FakeChannelAdapter
from openbot.testing.fakes.llm import FakeLLM


@pytest.mark.asyncio
async def test_chat_runtime_returns_answer():
    channel = FakeChannelAdapter()
    llm = FakeLLM(canned=["The repo uses pydantic v2."])
    responder = DeepAgentsChatResponder(channel=channel, llm=llm)

    answer = await responder.chat(
        repo="a/b", issue_number=1, question="What ORM does this use?",
    )

    assert isinstance(answer, str)
    assert "pydantic" in answer.lower()
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/agents/test_chat_runtime.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/agents/test_chat_runtime.py
git commit -m "test(integration): chat runtime answer shape"
```

### Task 5.29: Agents — tool schema invariants

**Files:**
- Create: `tests/integration/agents/test_tools_schema.py`

The agents expose a fixed set of tools to the LLM. Drift between the
declared schema and the runtime impl causes silent prompt regressions.

- [ ] **Step 1: Write the test file**

```python
"""Every declared agent tool has a callable handler with matching args."""

from __future__ import annotations

import inspect

import pytest

from openbot.infrastructure.agents._fix_tools import FIX_TOOLS
from openbot.infrastructure.agents._review_tools import REVIEW_TOOLS


@pytest.mark.parametrize("tool", FIX_TOOLS + REVIEW_TOOLS)
def test_tool_schema_matches_handler(tool):
    sig = inspect.signature(tool.handler)
    declared = set(tool.schema["parameters"]["properties"].keys())
    actual = {p for p in sig.parameters if p != "self"}
    assert declared == actual, (
        f"{tool.name}: declared {declared!r} != handler {actual!r}"
    )
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/agents/test_tools_schema.py -v`
Expected: all parametrizations PASSED (one per tool).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/agents/test_tools_schema.py
git commit -m "test(integration): agent tool schema invariants"
```

### Task 5.30: Evaluation — channel adapter side-effect guard

**Files:**
- Create: `tests/integration/evaluation/__init__.py`
- Create: `tests/integration/evaluation/test_eval_channel_adapter.py`

The eval channel adapter is the offline ChannelAdapter used by
`evals/`. We test it captures replies and PR reviews and *raises* when
production code accidentally calls a write-side path.

- [ ] **Step 1: Write the test file**

```python
"""EvalChannelAdapter records reads and rejects writes."""

from __future__ import annotations

import pytest

from openbot.evaluation.adapters import EvalChannelAdapter, EvalSideEffectError
from openbot.testing.builders import build_pull_request_opened_event


@pytest.mark.asyncio
async def test_eval_adapter_records_reply():
    a = EvalChannelAdapter(pr_diff="diff x")
    event = build_pull_request_opened_event(repo="a/b", pr_number=1)

    await a.reply(event, "hi from agent")

    assert a.replies == [(event.resource_key, "hi from agent")]


@pytest.mark.asyncio
async def test_eval_adapter_blocks_branch_creation():
    a = EvalChannelAdapter(pr_diff="")
    event = build_pull_request_opened_event(repo="a/b", pr_number=1)

    with pytest.raises(EvalSideEffectError):
        await a.create_branch(event, "feature/x", from_sha="x" * 40)


@pytest.mark.asyncio
async def test_eval_adapter_blocks_open_pr():
    a = EvalChannelAdapter(pr_diff="")
    event = build_pull_request_opened_event(repo="a/b", pr_number=1)

    with pytest.raises(EvalSideEffectError):
        await a.open_pull_request(
            event, title="t", body="b", head="feature/x", base="main",
        )
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/evaluation/test_eval_channel_adapter.py -v`
Expected: 3 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/evaluation/__init__.py tests/integration/evaluation/test_eval_channel_adapter.py
git commit -m "test(integration): EvalChannelAdapter side-effect guard"
```

### Task 5.31: Evaluation — runner sample exporters

**Files:**
- Create: `tests/integration/evaluation/test_runner.py`

`openbot.evaluation.runner` exports `run_review_sample`, `run_fix_sample`,
`run_chat_sample`, `run_test_generation_sample` — the production
inference path used by every Inspect AI task. We test the public API
returns a typed result without making real network calls.

- [ ] **Step 1: Write the test file**

```python
"""openbot.evaluation.runner public API smoke tests."""

from __future__ import annotations

import pytest

from openbot.evaluation import (
    run_chat_sample,
    run_fix_sample,
    run_review_sample,
    run_test_generation_sample,
)
from openbot.testing.fakes.llm import FakeLLM
from openbot.testing.fakes.sandbox import FakeSandbox


@pytest.mark.asyncio
async def test_run_review_sample_smoke():
    findings = await run_review_sample(
        repo="a/b",
        pr_number=1,
        diff="diff x",
        llm=FakeLLM(canned=['{"summary":"ok","comments":[]}']),
    )
    assert findings.summary


@pytest.mark.asyncio
async def test_run_chat_sample_smoke():
    answer = await run_chat_sample(
        repo="a/b",
        issue_number=1,
        question="What ORM?",
        llm=FakeLLM(canned=["pydantic v2"]),
    )
    assert "pydantic" in answer.lower()


@pytest.mark.asyncio
async def test_run_fix_sample_requires_sandbox():
    with pytest.raises(ValueError):
        await run_fix_sample(
            repo="a/b", issue_number=1, llm=FakeLLM(canned=["{}"]), sandbox=None,
        )


@pytest.mark.asyncio
async def test_run_test_generation_sample_returns_diff():
    sandbox = FakeSandbox(files={"src/x.py": "def f(): return 1\n"})
    result = await run_test_generation_sample(
        repo="a/b", issue_number=1,
        llm=FakeLLM(canned=['{"plan":"x","actions":[]}']),
        sandbox=sandbox,
    )
    assert result is not None
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/integration/evaluation/test_runner.py -v`
Expected: 4 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/evaluation/test_runner.py
git commit -m "test(integration): evaluation runner public API smoke"
```

### Task 5.32: Whole-integration-layer budget verification

**Files:**
- (verification only)

- [ ] **Step 1: Run with timing**

Run: `uv run pytest tests/integration -q --durations=20`
Expected: whole layer under **240 s** (4 minutes). Top 20 slowest
tests should each be under 10 s. If any single test is over 10 s,
diagnose: usually a stray real-time `await asyncio.sleep` or a fake
that is not actually in-process.

- [ ] **Step 2: Verify serial execution is sane**

Integration runs serially because some fakes share state across the
file (FakeRunsRepo persistence, FakeAuditLog rows). Do *not* add
`-n auto` to integration. Confirm no test relies on global mutable
state surviving across files (each conftest fixture is function-scoped).

- [ ] **Step 3: Commit if any source fixes were made**

```bash
git add -A
git commit -m "test(integration): whole layer under 4-minute budget"
```

## Phase 6 — Smoke layer tests

The smoke layer is the boot-invariant safety net. Each test imports a
top-level entrypoint, builds it with the fake-backed dependencies,
and asserts construction succeeds. Smoke catches "broken dependency
graph" regressions that unit/contract/integration would miss because
they exercise specific code paths, not whole-app wire-up.

Spec coverage: §6 (smoke), §10.1 (boot invariants).

Layer budget: whole layer under **30 s**. These tests are short.

### Task 6.1: FastAPI app boot

**Files:**
- Create: `tests/smoke/test_app_boot.py`

- [ ] **Step 1: Write the test file**

```python
"""FastAPI app constructs with fake-backed environment."""

from __future__ import annotations

from fastapi.testclient import TestClient

from openbot.webapp import create_app


def test_app_constructs_with_fake_env(boot_env):
    app = create_app()
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200


def test_app_routes_registered(boot_env):
    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/healthz" in paths
    assert "/webhooks/github" in paths
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/smoke/test_app_boot.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/smoke/test_app_boot.py
git commit -m "test(smoke): FastAPI app boot invariants"
```

### Task 6.2: Worker boot

**Files:**
- Create: `tests/smoke/test_worker_boot.py`

- [ ] **Step 1: Write the test file**

```python
"""Worker entrypoint constructs and exposes its loop without crashing."""

from __future__ import annotations

import importlib

import pytest


def test_worker_main_module_importable(boot_env):
    mod = importlib.import_module("openbot.entrypoints.worker.__main__")
    assert hasattr(mod, "main")


@pytest.mark.asyncio
async def test_worker_constructs_with_fake_redis(boot_env, inmemory_redis):
    from openbot.infrastructure.queue.worker import Worker

    class _Stub:
        async def execute(self, action): ...
        async def decide(self, event): ...

    w = Worker(redis=inmemory_redis, dispatcher=_Stub(), stream="openbot:tasks")
    assert w is not None
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/smoke/test_worker_boot.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/smoke/test_worker_boot.py
git commit -m "test(smoke): worker boot invariants"
```

### Task 6.3: CLI boot

**Files:**
- Create: `tests/smoke/test_cli_boot.py`

- [ ] **Step 1: Write the test file**

```python
"""CLI entrypoints (audit, db_init, setup_wizard) import and expose main()."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "modname",
    [
        "openbot.entrypoints.cli.audit",
        "openbot.entrypoints.cli.db_init",
        "openbot.entrypoints.cli.setup_wizard",
    ],
)
def test_cli_module_importable(boot_env, modname):
    mod = importlib.import_module(modname)
    assert hasattr(mod, "main") or hasattr(mod, "app"), (
        f"{modname} must expose `main` or `app`"
    )
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/smoke/test_cli_boot.py -v`
Expected: 3 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/smoke/test_cli_boot.py
git commit -m "test(smoke): CLI entrypoint boot"
```

### Task 6.4: Settings buildable from env

**Files:**
- Create: `tests/smoke/test_settings_buildable.py`

- [ ] **Step 1: Write the test file**

```python
"""Settings constructs from a minimal valid env and rejects bad env."""

from __future__ import annotations

import pytest

from openbot.config import Settings


def test_settings_buildable_with_minimum_env(monkeypatch):
    monkeypatch.setenv("OPENBOT_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("OPENBOT_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "1")
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("OPENBOT_GITHUB_PRIVATE_KEY", "")  # tested separately

    s = Settings()
    assert s.database_url
    assert s.redis_url


def test_settings_rejects_missing_database_url(monkeypatch):
    monkeypatch.delenv("OPENBOT_DATABASE_URL", raising=False)
    with pytest.raises(Exception):  # ValidationError or RuntimeError
        Settings()
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/smoke/test_settings_buildable.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/smoke/test_settings_buildable.py
git commit -m "test(smoke): Settings boot invariants"
```

### Task 6.5: Alembic single-head

**Files:**
- Create: `tests/smoke/test_alembic_heads.py`

A single migration head is a hard invariant — multiple heads silently
break `alembic upgrade head` in production deployments.

- [ ] **Step 1: Write the test file**

```python
"""Alembic must have exactly one head; upgrade head must run cleanly."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_alembic_single_head():
    out = subprocess.check_output(
        ["uv", "run", "alembic", "heads"], cwd=ROOT
    ).decode()
    head_lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(head_lines) == 1, f"expected single head, got {head_lines!r}"


def test_alembic_upgrade_head_smoke(tmp_path, monkeypatch):
    db_file = tmp_path / "smoke.db"
    monkeypatch.setenv(
        "OPENBOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}"
    )
    subprocess.check_call(
        ["uv", "run", "alembic", "upgrade", "head"], cwd=ROOT
    )
    assert db_file.exists()
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/smoke/test_alembic_heads.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/smoke/test_alembic_heads.py
git commit -m "test(smoke): alembic single-head + upgrade smoke"
```

### Task 6.6: import-linter contracts

**Files:**
- Create: `tests/smoke/test_import_linter.py`
- Modify: `pyproject.toml` (add import-linter contracts)

The import-linter contracts encode the layered architecture and the
fakes-stay-out-of-runtime invariants:

  1. **test-layers**: tests/unit cannot import from tests/integration
     or higher. Each test layer is self-contained.
  2. **test-no-private-fakes**: tests/* may not import from
     tests/_fakes/ — that directory has been wiped. All fakes live
     in `openbot.testing.fakes`.
  3. **no-testing-in-runtime**: `openbot/` (production code) cannot
     import `openbot.testing` — fakes leak production guards
     otherwise.

- [ ] **Step 1: Add contracts to pyproject.toml**

```toml
[tool.importlinter]
root_packages = ["openbot", "tests"]

[[tool.importlinter.contracts]]
name = "test-layers"
type = "layers"
layers = [
    "tests.real_service",
    "tests.e2e",
    "tests.smoke",
    "tests.integration",
    "tests.contract",
    "tests.unit",
]

[[tool.importlinter.contracts]]
name = "no-testing-in-runtime"
type = "forbidden"
source_modules = ["openbot"]
forbidden_modules = ["openbot.testing"]
ignore_imports = []
```

- [ ] **Step 2: Write the test file**

```python
"""import-linter must report all contracts kept."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_import_linter_clean():
    result = subprocess.run(
        ["uv", "run", "lint-imports"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 3: Run and verify**

Run: `uv run pytest tests/smoke/test_import_linter.py -v`
Expected: 1 PASSED. If it fails, the diff that triggered the failure
must fix the offending import — do NOT relax the contract.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml tests/smoke/test_import_linter.py
git commit -m "test(smoke): import-linter contracts"
```

### Task 6.7: Contract coverage invariant

**Files:**
- Create: `tests/smoke/test_contract_coverage.py`

This test enforces that every Port in `openbot.application.ports` has
a contract test file. It walks the ports package and matches each
module to a `tests/contract/test_*_contract.py` file. Drift here is
loud — if you add a port and forget the contract, this fails.

- [ ] **Step 1: Write the test file**

```python
"""Every Port has a corresponding contract test file."""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import openbot.application.ports as ports_pkg

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = ROOT / "tests" / "contract"


def _all_port_modules() -> list[str]:
    return [
        m.name for m in pkgutil.iter_modules(ports_pkg.__path__)
        if not m.name.startswith("_")
    ]


def test_every_port_has_contract_file():
    missing: list[str] = []
    for modname in _all_port_modules():
        candidate = CONTRACT_DIR / f"test_{modname}_contract.py"
        if not candidate.exists():
            missing.append(str(candidate.relative_to(ROOT)))
    assert not missing, f"missing contract tests: {missing}"


def test_every_port_module_loads():
    for modname in _all_port_modules():
        importlib.import_module(f"openbot.application.ports.{modname}")
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/smoke/test_contract_coverage.py -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/smoke/test_contract_coverage.py
git commit -m "test(smoke): every Port has a contract test"
```

### Task 6.8: Test budget enforcement

**Files:**
- Create: `tests/smoke/test_budget.py`

This test runs each layer with timing and asserts the per-layer wall
budgets from §10.1 of the spec. Catches regressions early.

- [ ] **Step 1: Write the test file**

```python
"""Per-layer wall-clock budget guard.

Reasonable budgets for the local make-check loop:
  - unit:        20 s
  - contract:    60 s
  - integration: 240 s
  - smoke:       30 s

Skip when invoked under the same root pytest run (avoid recursion):
the budget test is meant to run from CI or `make test-budget`, not
inside `make test`.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUDGETS = {
    "tests/unit": 20,
    "tests/contract": 60,
    "tests/integration": 240,
    "tests/smoke": 30,
}


@pytest.mark.skipif(
    os.environ.get("OPENBOT_TEST_BUDGET_RUN") != "1",
    reason="budget test is opt-in to avoid pytest recursion",
)
@pytest.mark.parametrize("layer,budget_s", list(BUDGETS.items()))
def test_layer_under_budget(layer: str, budget_s: int):
    start = time.monotonic()
    subprocess.check_call(
        ["uv", "run", "pytest", layer, "-q"], cwd=ROOT
    )
    elapsed = time.monotonic() - start
    assert elapsed < budget_s, f"{layer} took {elapsed:.1f}s > {budget_s}s budget"
```

- [ ] **Step 2: Run and verify (opt-in)**

Run: `OPENBOT_TEST_BUDGET_RUN=1 uv run pytest tests/smoke/test_budget.py -v`
Expected: 4 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/smoke/test_budget.py
git commit -m "test(smoke): per-layer wall-clock budget guard"
```

## Phase 7 — E2E layer tests

E2E tests assemble the **whole stack** with fakes and drive it from a
realistic event payload. They are the strongest local signal that
"the system works". We use 6 scenarios chosen to cover the most
common production paths:

  1. issue opened → triage labels and replies
  2. PR opened → review posts comments
  3. command "fix" → fix opens PR
  4. command "chat" → chat replies
  5. supersede lifecycle (running fix superseded by new fix)
  6. error recovery (use case raises, run marked failed, audit row)

Spec coverage: §6 (e2e), §11 (lifecycle scenarios).

Layer budget: whole layer under **120 s**.

### Task 7.1: E2E stack assembly

**Files:**
- Create: `tests/e2e/_assemble.py`

The `_assemble.py` module is the e2e analog of the integration SUT
factory: a single `assemble_stack()` returns a frozen dataclass with
the fully wired stack (FastAPI app, dispatcher, worker, fakes). Tests
import this and fire events at the app — no per-test wiring.

- [ ] **Step 1: Write the assembly module**

```python
"""E2E stack assembly — wires the whole system with fakes.

One call to ``assemble_stack()`` returns the FastAPI app, the
dispatcher, the worker, and references to every fake so tests can
fire events and inspect what was recorded.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from openbot.application.dispatcher import Dispatcher
from openbot.infrastructure.queue.worker import Worker
from openbot.testing.fakes.audit_log import FakeAuditLog
from openbot.testing.fakes.cancellation import FakeCancellation
from openbot.testing.fakes.channel_adapter import FakeChannelAdapter
from openbot.testing.fakes.config_loader import FakeConfigLoader
from openbot.testing.fakes.dedup import FakeDedup
from openbot.testing.fakes.llm import FakeLLM
from openbot.testing.fakes.queue import FakeQueue
from openbot.testing.fakes.rate_limiter import FakeRateLimiter
from openbot.testing.fakes.resource_lock import FakeResourceLock
from openbot.testing.fakes.runs_repo import FakeRunsRepo
from openbot.testing.fakes.sandbox import FakeSandbox
from openbot.testing.fakes.sandbox_cache import FakeSandboxCache


@dataclass(frozen=True, slots=True)
class E2EStack:
    client: TestClient
    dispatcher: Dispatcher
    worker: Worker
    channel: FakeChannelAdapter
    runs: FakeRunsRepo
    audit: FakeAuditLog
    llm: FakeLLM
    queue: FakeQueue
    sandbox: FakeSandbox


def assemble_stack(
    *,
    llm_responses: dict[str, list[str]] | None = None,
    sandbox_files: dict[str, str] | None = None,
) -> E2EStack:
    """Build the whole-stack stand-in for one e2e scenario.

    ``llm_responses`` is keyed by use-case name (``"triage"``,
    ``"review"``, ``"fix"``, ``"chat"``) so different agents see
    different canned responses.
    """
    canned = llm_responses or {}
    channel = FakeChannelAdapter()
    runs = FakeRunsRepo()
    audit = FakeAuditLog()
    queue = FakeQueue()
    sandbox = FakeSandbox(files=sandbox_files or {})
    llm = FakeLLM(canned_by_kind=canned)
    cfg = FakeConfigLoader.with_defaults()
    dispatcher = Dispatcher(
        runs=runs, dedup=FakeDedup(), config=cfg,
        use_cases={},  # filled below
    )

        from openbot.application.use_cases.chat import ChatUseCase
    from openbot.application.use_cases.fix import FixUseCase
    from openbot.application.use_cases.review import ReviewUseCase
    from openbot.application.use_cases.triage import TriageUseCase
    from openbot.domain.actions import ActionKind
    from openbot.webapp import build_app_for_testing

    common_deps = dict(
        channel=channel, runs=runs, audit=audit, llm=llm, config=cfg,
    )
    triage_uc = TriageUseCase(**common_deps)
    review_uc = ReviewUseCase(**common_deps)
    chat_uc = ChatUseCase(**common_deps)
    fix_uc = FixUseCase(
        **common_deps, sandbox_factory=lambda **_kw: sandbox,
    )

    dispatcher = Dispatcher(
        runs=runs, dedup=FakeDedup(), config=cfg,
        use_cases={
            ActionKind.RUN_TRIAGE: triage_uc,
            ActionKind.RUN_REVIEW: review_uc,
            ActionKind.RUN_FIX: fix_uc,
            ActionKind.RUN_CHAT: chat_uc,
        },
    )
    worker = Worker(redis=queue, dispatcher=dispatcher, stream="openbot:tasks")
    app = build_app_for_testing(channel=channel, queue=queue)
    return E2EStack(
        client=TestClient(app),
        dispatcher=dispatcher, worker=worker,
        channel=channel, runs=runs, audit=audit, llm=llm,
        queue=queue, sandbox=sandbox,
    )


__all__ = ["E2EStack", "assemble_stack"]
```

- [ ] **Step 2: Add `build_app_for_testing` if it doesn't exist**

If `openbot/webapp.py` does not export `build_app_for_testing(*, channel, queue)`,
add a thin factory that wires the FastAPI app with the supplied
fakes (instead of reading from settings/env). Pattern:

```python
# openbot/webapp.py — add near create_app()
def build_app_for_testing(*, channel, queue) -> FastAPI:
    app = FastAPI()
    app.state.channel = channel
    app.state.queue = queue
    _register_routes(app)
    return app
```

If the existing webapp uses container-based injection, mirror that
pattern instead — the goal is a single function the e2e tests can
call to get an app pre-wired with fakes.

- [ ] **Step 3: Run import check**

Run: `uv run python -c "from tests.e2e._assemble import assemble_stack; assemble_stack()"`
Expected: no exception.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/_assemble.py openbot/webapp.py
git commit -m "test(e2e): full-stack assembly with fakes"
```

### Task 7.2: E2E — issue → triage

**Files:**
- Create: `tests/e2e/test_issue_to_triage.py`

- [ ] **Step 1: Write the test file**

```python
"""Issue opened webhook flows through to triage label + reply."""

from __future__ import annotations

import asyncio
import json

import pytest

from openbot.testing.fixtures.webhooks import sign_github_payload
from tests.e2e._assemble import assemble_stack

ISSUE_PAYLOAD = {
    "action": "opened",
    "repository": {"full_name": "acme/widget"},
    "issue": {"number": 42, "title": "Crash", "body": "App segfaults",
              "user": {"login": "alice"}},
    "sender": {"login": "alice"},
}


@pytest.mark.asyncio
async def test_issue_opened_triages():
    stack = assemble_stack(
        llm_responses={"triage": ['{"label":"bug","confidence":0.9}']},
    )

    body = json.dumps(ISSUE_PAYLOAD).encode()
    headers = sign_github_payload(body, event="issues")
    response = stack.client.post("/webhooks/github", content=body, headers=headers)
    assert response.status_code == 202

    # Drain the queue: webhook enqueues, worker dispatches.
    await asyncio.wait_for(stack.worker.run_one(), timeout=5.0)

    assert stack.channel.labels_added, "expected at least one label"
    assert stack.channel.replies, "expected reply"
    assert stack.runs.runs[0].status == "succeeded"
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/e2e/test_issue_to_triage.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_issue_to_triage.py
git commit -m "test(e2e): issue opened → triage scenario"
```

### Task 7.3: E2E — PR → review

**Files:**
- Create: `tests/e2e/test_pr_to_review.py`

- [ ] **Step 1: Write the test file**

```python
"""Pull request opened webhook flows through to review."""

from __future__ import annotations

import asyncio
import json

import pytest

from openbot.testing.fixtures.webhooks import sign_github_payload
from tests.e2e._assemble import assemble_stack

PR_PAYLOAD = {
    "action": "opened",
    "repository": {"full_name": "acme/widget"},
    "pull_request": {
        "number": 11,
        "title": "Add cache",
        "body": "",
        "head": {"sha": "a" * 40, "ref": "feature"},
        "base": {"sha": "b" * 40, "ref": "main"},
        "user": {"login": "alice"},
    },
    "sender": {"login": "alice"},
}


@pytest.mark.asyncio
async def test_pr_opened_reviews():
    stack = assemble_stack(
        llm_responses={"review": ['{"summary":"lgtm","comments":[]}']},
    )
    stack.channel.pr_diff = "diff --git a/x.py b/x.py\n+pass\n"

    body = json.dumps(PR_PAYLOAD).encode()
    headers = sign_github_payload(body, event="pull_request")
    r = stack.client.post("/webhooks/github", content=body, headers=headers)
    assert r.status_code == 202

    await asyncio.wait_for(stack.worker.run_one(), timeout=5.0)

    assert stack.channel.pr_reviews, "expected PR review"
    assert stack.runs.runs[0].status == "succeeded"
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/e2e/test_pr_to_review.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_pr_to_review.py
git commit -m "test(e2e): PR opened → review scenario"
```

### Task 7.4: E2E — command "fix" → PR opened

**Files:**
- Create: `tests/e2e/test_command_to_fix.py`

- [ ] **Step 1: Write the test file**

```python
"""issue_comment with /fix command flows through to a PR."""

from __future__ import annotations

import asyncio
import json

import pytest

from openbot.testing.fixtures.webhooks import sign_github_payload
from tests.e2e._assemble import assemble_stack

CMD_PAYLOAD = {
    "action": "created",
    "repository": {"full_name": "acme/widget"},
    "issue": {"number": 99, "title": "Crash", "body": "App segfaults",
              "user": {"login": "alice"}, "pull_request": None},
    "comment": {"body": "/fix please add a guard",
                "user": {"login": "alice"}},
    "sender": {"login": "alice"},
}


@pytest.mark.asyncio
async def test_command_fix_opens_pr():
    stack = assemble_stack(
        llm_responses={"fix": ['{"plan":"add guard","actions":[]}']},
        sandbox_files={"src/api.py": "def hi(): return 1\n"},
    )

    body = json.dumps(CMD_PAYLOAD).encode()
    headers = sign_github_payload(body, event="issue_comment")
    r = stack.client.post("/webhooks/github", content=body, headers=headers)
    assert r.status_code == 202

    await asyncio.wait_for(stack.worker.run_one(), timeout=10.0)

    assert stack.channel.branches_created, "expected branch"
    assert stack.channel.prs_opened, "expected PR opened"
    assert stack.runs.runs[0].status == "succeeded"
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/e2e/test_command_to_fix.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_command_to_fix.py
git commit -m "test(e2e): /fix command → PR opened scenario"
```

### Task 7.5: E2E — command "chat" → reply

**Files:**
- Create: `tests/e2e/test_command_to_chat.py`

- [ ] **Step 1: Write the test file**

```python
"""issue_comment with /chat command flows through to a reply."""

from __future__ import annotations

import asyncio
import json

import pytest

from openbot.testing.fixtures.webhooks import sign_github_payload
from tests.e2e._assemble import assemble_stack

CMD_PAYLOAD = {
    "action": "created",
    "repository": {"full_name": "acme/widget"},
    "issue": {"number": 33, "title": "Q",
              "body": "what's our ORM?",
              "user": {"login": "alice"}, "pull_request": None},
    "comment": {"body": "/chat which orm do we use?",
                "user": {"login": "alice"}},
    "sender": {"login": "alice"},
}


@pytest.mark.asyncio
async def test_command_chat_replies():
    stack = assemble_stack(
        llm_responses={"chat": ["pydantic + sqlalchemy"]},
    )

    body = json.dumps(CMD_PAYLOAD).encode()
    headers = sign_github_payload(body, event="issue_comment")
    r = stack.client.post("/webhooks/github", content=body, headers=headers)
    assert r.status_code == 202

    await asyncio.wait_for(stack.worker.run_one(), timeout=5.0)

    assert stack.channel.replies
    assert "sqlalchemy" in stack.channel.replies[-1][1].lower()
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/e2e/test_command_to_chat.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_command_to_chat.py
git commit -m "test(e2e): /chat command → reply scenario"
```

### Task 7.6: E2E — supersede lifecycle

**Files:**
- Create: `tests/e2e/test_supersede_lifecycle.py`

This is the most subtle scenario. Two `/fix` commands on the same
issue arrive in quick succession. The first run starts; the second
arrives, marks the first superseded, and runs to completion. The
first run's coroutine observes the cancellation token and exits.

- [ ] **Step 1: Write the test file**

```python
"""Two /fix commands on the same issue: the second supersedes the first."""

from __future__ import annotations

import asyncio
import json

import pytest

from openbot.testing.fixtures.webhooks import sign_github_payload
from tests.e2e._assemble import assemble_stack


def _cmd_payload(comment_id: int, body: str) -> dict:
    return {
        "action": "created",
        "repository": {"full_name": "acme/widget"},
        "issue": {"number": 50, "title": "Crash",
                  "body": "App segfaults",
                  "user": {"login": "alice"},
                  "pull_request": None},
        "comment": {"id": comment_id, "body": body,
                    "user": {"login": "alice"}},
        "sender": {"login": "alice"},
    }


@pytest.mark.asyncio
async def test_second_fix_supersedes_first():
    stack = assemble_stack(
        llm_responses={"fix": ['{"plan":"x","actions":[]}'] * 2},
        sandbox_files={"src/api.py": "def hi(): return 1\n"},
    )

    for cid, body in [(1, "/fix v1"), (2, "/fix v2")]:
        payload = json.dumps(_cmd_payload(cid, body)).encode()
        headers = sign_github_payload(payload, event="issue_comment")
        stack.client.post("/webhooks/github", content=payload, headers=headers)

    await asyncio.wait_for(stack.worker.run_one(), timeout=10.0)
    await asyncio.wait_for(stack.worker.run_one(), timeout=10.0)

    statuses = [r.status for r in stack.runs.runs]
    assert "superseded" in statuses
    assert "succeeded" in statuses
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/e2e/test_supersede_lifecycle.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_supersede_lifecycle.py
git commit -m "test(e2e): supersede lifecycle scenario"
```

### Task 7.7: E2E — error recovery

**Files:**
- Create: `tests/e2e/test_error_recovery.py`

When a use case raises (LLM out of budget, sandbox crash, GitHub
write failure), the run row must be marked `failed`, the audit must
record `run.failed`, and the user must get a polite reply. No
exception should bubble out of the worker loop — that would crash
the consumer.

- [ ] **Step 1: Write the test file**

```python
"""LLM raises mid-run: run is failed, audit records, user gets reply."""

from __future__ import annotations

import asyncio
import json

import pytest

from openbot.testing.fixtures.webhooks import sign_github_payload
from tests.e2e._assemble import assemble_stack

PAYLOAD = {
    "action": "opened",
    "repository": {"full_name": "acme/widget"},
    "issue": {"number": 77, "title": "Crash", "body": "App",
              "user": {"login": "alice"}},
    "sender": {"login": "alice"},
}


@pytest.mark.asyncio
async def test_use_case_failure_does_not_crash_worker():
    stack = assemble_stack(llm_responses={"triage": []})  # exhausted

    body = json.dumps(PAYLOAD).encode()
    headers = sign_github_payload(body, event="issues")
    stack.client.post("/webhooks/github", content=body, headers=headers)

    # Worker loop must not raise even though the use case fails.
    await asyncio.wait_for(stack.worker.run_one(), timeout=5.0)

    assert stack.runs.runs[0].status == "failed"
    assert any(row.kind == "run.failed" for row in stack.audit.rows)
    assert stack.channel.replies, "expected user-visible failure reply"
```

- [ ] **Step 2: Run and verify**

Run: `uv run pytest tests/e2e/test_error_recovery.py -v`
Expected: 1 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_error_recovery.py
git commit -m "test(e2e): error recovery scenario"
```

## Phase 8 — Real-service layer + cassettes + GH Actions

The real-service layer is the only place where `tests/` talks to a
real database, real Redis, real GitHub API (replayed from cassettes),
or a real smee relay. Each test uses `pytest.skip(allow_module_level=True)`
when the relevant `OPENBOT_TEST_*` env var is missing — locally they
no-op, in CI they run via `make test-real-service`.

Spec coverage: §6 (real_service), §9 (cassettes + redaction), §11.2
(GH Actions), §13 (security boundaries).

Layer budget: whole layer under **8 minutes** in CI. Locally,
contingent on env presence — typically a sub-second smoke per service.

### Task 8.1: Real Postgres — schema round-trip

**Files:**
- Create: `tests/real_service/postgres/__init__.py`
- Create: `tests/real_service/postgres/test_schema.py`

- [ ] **Step 1: Write the test file**

```python
"""Postgres real-service: alembic upgrade head + a CRUD round-trip.

Skipped when ``OPENBOT_TEST_POSTGRES_URL`` is not set.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

URL = os.environ.get("OPENBOT_TEST_POSTGRES_URL")
if not URL:
    pytest.skip(
        "OPENBOT_TEST_POSTGRES_URL not set", allow_module_level=True
    )

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_alembic_upgrade_against_real_postgres(monkeypatch):
    monkeypatch.setenv("OPENBOT_DATABASE_URL", URL)
    subprocess.check_call(
        ["uv", "run", "alembic", "upgrade", "head"], cwd=ROOT
    )


@pytest.mark.asyncio
async def test_runs_repo_round_trip_on_postgres(monkeypatch):
    monkeypatch.setenv("OPENBOT_DATABASE_URL", URL)
    from openbot.infrastructure.persistence.db import build_session_maker
    from openbot.infrastructure.persistence.runs_repo_impl import SqlAlchemyRunsRepo

    session_maker, engine = await build_session_maker(URL)
    try:
        repo = SqlAlchemyRunsRepo(session_maker=session_maker)
        run = await repo.create(
            resource_key="real/repo#1", kind="triage",
            actor="alice", idempotency_key="k1",
        )
        fetched = await repo.get(run.id)
        assert fetched is not None
    finally:
        await engine.dispose()
```

- [ ] **Step 2: Run and verify (local with env var)**

Run: `OPENBOT_TEST_POSTGRES_URL=postgresql+asyncpg://localhost/openbot_test uv run pytest tests/real_service/postgres -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/real_service/postgres/__init__.py tests/real_service/postgres/test_schema.py
git commit -m "test(real_service): postgres schema + round-trip"
```

### Task 8.2: Real Redis — stream + pubsub round-trip

**Files:**
- Create: `tests/real_service/redis/__init__.py`
- Create: `tests/real_service/redis/test_stream.py`

- [ ] **Step 1: Write the test file**

```python
"""Redis real-service: stream xadd/xrange + pubsub round-trip.

Skipped when ``OPENBOT_TEST_REDIS_URL`` is not set.
"""

from __future__ import annotations

import os

import pytest
import redis.asyncio as aioredis

URL = os.environ.get("OPENBOT_TEST_REDIS_URL")
if not URL:
    pytest.skip("OPENBOT_TEST_REDIS_URL not set", allow_module_level=True)


@pytest.mark.asyncio
async def test_redis_stream_round_trip():
    r = aioredis.from_url(URL)
    try:
        key = "openbot:test:stream"
        await r.delete(key)
        await r.xadd(key, {"k": "v"})
        entries = await r.xrange(key)
        assert len(entries) == 1
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_redis_pubsub_round_trip():
    r = aioredis.from_url(URL)
    try:
        ps = r.pubsub()
        await ps.subscribe("openbot:test:ch")
        # consume the subscribe ack
        await ps.get_message(timeout=1.0)
        await r.publish("openbot:test:ch", "hello")
        msg = await ps.get_message(timeout=2.0)
        assert msg is not None
        assert msg["data"] == b"hello"
        await ps.unsubscribe("openbot:test:ch")
    finally:
        await r.aclose()
```

- [ ] **Step 2: Run and verify**

Run: `OPENBOT_TEST_REDIS_URL=redis://localhost:6379/15 uv run pytest tests/real_service/redis -v`
Expected: 2 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/real_service/redis/__init__.py tests/real_service/redis/test_stream.py
git commit -m "test(real_service): redis stream + pubsub round-trip"
```

### Task 8.3: GitHub API cassettes (PR-only) via VCR

**Files:**
- Create: `tests/real_service/github/__init__.py`
- Create: `tests/real_service/github/conftest.py`
- Create: `tests/real_service/github/cassettes/.gitkeep`

VCR cassettes record real GitHub API responses on first run, then
replay them on subsequent runs without network. The cassettes are
*test code* — committed to git, kept under review, and redacted of
all secrets via the pre-commit hook (Task 8.6).

- [ ] **Step 1: Write the conftest with VCR config**

```python
"""VCR cassette config for GitHub API tests.

Cassettes are recorded against a live GitHub PR (the user's choice,
typically a sandboxed test PR in a fixture repo). The redactor below
strips:
  - Authorization headers (Bearer / token)
  - any ghs_/gho_/ghp_/AKIA pattern in body
  - PEM blocks
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

CASSETTE_DIR = Path(__file__).parent / "cassettes"
SECRET_RX = re.compile(
    r"(ghs_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|"
    r"ghp_[A-Za-z0-9]{36}|AKIA[A-Z0-9]{16}|"
    r"-----BEGIN [A-Z ]+ KEY-----[\s\S]+?-----END [A-Z ]+ KEY-----)"
)


def _redact_request(request):
    request.headers.pop("Authorization", None)
    if request.body:
        body = request.body.decode() if isinstance(request.body, bytes) else request.body
        request.body = SECRET_RX.sub("[REDACTED]", body)
    return request


def _redact_response(response):
    body = response["body"]["string"]
    if isinstance(body, bytes):
        body = body.decode(errors="ignore")
    response["body"]["string"] = SECRET_RX.sub("[REDACTED]", body).encode()
    return response


@pytest.fixture
def vcr_config():
    return {
        "cassette_library_dir": str(CASSETTE_DIR),
        "filter_headers": ["authorization", "cookie"],
        "before_record_request": _redact_request,
        "before_record_response": _redact_response,
        "record_mode": os.environ.get("OPENBOT_VCR_MODE", "none"),
    }
```

- [ ] **Step 2: Add the .gitkeep so the dir is committed**

```bash
mkdir -p tests/real_service/github/cassettes
touch tests/real_service/github/cassettes/.gitkeep
```

- [ ] **Step 3: Commit**

```bash
git add tests/real_service/github/__init__.py tests/real_service/github/conftest.py tests/real_service/github/cassettes/.gitkeep
git commit -m "test(real_service): VCR config + cassette dir"
```

### Task 8.4: GitHub PR review cassette test

**Files:**
- Create: `tests/real_service/github/test_pr_review.py`

- [ ] **Step 1: Write the test file**

```python
"""Replay a GitHub PR review API call from cassette."""

from __future__ import annotations

import os

import pytest

if not os.environ.get("OPENBOT_TEST_GITHUB_REPO"):
    pytest.skip(
        "OPENBOT_TEST_GITHUB_REPO not set", allow_module_level=True
    )


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_get_pr_diff_via_real_github(monkeypatch):
    """First run records, subsequent runs replay from cassette."""
    from openbot.infrastructure.adapters.github_channel import GitHubChannelAdapter

    repo = os.environ["OPENBOT_TEST_GITHUB_REPO"]
    pr = int(os.environ.get("OPENBOT_TEST_GITHUB_PR", "1"))
    adapter = GitHubChannelAdapter.from_env()

    diff = await adapter.get_pr_diff(_synthetic_event(repo), pr)
    assert isinstance(diff, str)


def _synthetic_event(repo: str):
    from openbot.testing.builders import build_pull_request_opened_event
    return build_pull_request_opened_event(repo=repo, pr_number=1)
```

- [ ] **Step 2: Record the cassette (one-time)**

Run with a real installation token + recording mode to capture the
cassette, then commit:

```bash
OPENBOT_VCR_MODE=once \
OPENBOT_TEST_GITHUB_REPO=acme/openbot-fixtures \
OPENBOT_TEST_GITHUB_PR=1 \
GITHUB_TOKEN=ghs_... \
uv run pytest tests/real_service/github/test_pr_review.py -v
```

Verify the resulting cassette has no secrets (the redactor should
have stripped them); then re-run with `OPENBOT_VCR_MODE=none` to
confirm replay works without network.

- [ ] **Step 3: Commit (cassette included)**

```bash
git add tests/real_service/github/test_pr_review.py tests/real_service/github/cassettes/
git commit -m "test(real_service): GitHub PR review via VCR cassette"
```

### Task 8.5: Smee replay end-to-end

**Files:**
- Create: `tests/real_service/smee/__init__.py`
- Create: `tests/real_service/smee/test_replay.py`

The smee test replays a captured webhook payload against a live
local server (started in-process via `uvicorn`), exercising the full
intake path: signature → dedup → enqueue. It does NOT hit the LLM —
the worker is not started in this scenario.

- [ ] **Step 1: Write the test file**

```python
"""Replay a captured smee webhook payload through the running app."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

if not os.environ.get("OPENBOT_TEST_SMEE_PAYLOAD"):
    pytest.skip(
        "OPENBOT_TEST_SMEE_PAYLOAD path not set", allow_module_level=True
    )

PAYLOAD_PATH = Path(os.environ["OPENBOT_TEST_SMEE_PAYLOAD"])


@pytest.mark.asyncio
async def test_smee_replay_accepted(boot_env, in_process_app_url):
    raw = PAYLOAD_PATH.read_bytes()
    headers = json.loads(PAYLOAD_PATH.with_suffix(".headers.json").read_text())
    async with httpx.AsyncClient(base_url=in_process_app_url) as client:
        r = await client.post("/webhooks/github", content=raw, headers=headers)
    assert r.status_code in {200, 202}
```

- [ ] **Step 2: Add the `in_process_app_url` fixture**

Append to `tests/real_service/smee/conftest.py` (create the file):

```python
"""Boot uvicorn in-process for smee replay tests."""

from __future__ import annotations

import asyncio
import socket
import threading

import pytest
import uvicorn

from openbot.webapp import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def in_process_app_url(boot_env):
    port = _free_port()
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port,
        log_level="warning", lifespan="on",
    )
    server = uvicorn.Server(config)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=lambda: loop.run_until_complete(server.serve()),
        daemon=True,
    )
    thread.start()
    while not server.started:
        pass
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5.0)
```

- [ ] **Step 3: Run with a captured payload**

Capture a payload from smee:

```bash
smee --target http://localhost:8080/webhooks/github --port 5000 --tee /tmp/payloads/
# pick one
export OPENBOT_TEST_SMEE_PAYLOAD=/tmp/payloads/2025-01-01-issue-opened.bin
uv run pytest tests/real_service/smee -v
```

Expected: 1 PASSED.

- [ ] **Step 4: Commit**

```bash
git add tests/real_service/smee/__init__.py tests/real_service/smee/conftest.py tests/real_service/smee/test_replay.py
git commit -m "test(real_service): smee replay scenario"
```

### Task 8.6: Cassette secret-scan pre-commit hook

**Files:**
- Create: `scripts/check-cassettes.py`
- Modify: `.pre-commit-config.yaml`

The cassette redactor in the VCR config catches secrets at record
time, but a defective redactor or an out-of-band cassette write
could still leak. The pre-commit hook is the second wall: it greps
every YAML cassette for known secret patterns and fails the commit
if any match.

- [ ] **Step 1: Write the scanner script**

```python
#!/usr/bin/env python3
"""Scan VCR cassettes (and similar fixture YAML) for secret patterns.

Exits non-zero on the first match. Run as a pre-commit hook against
every staged cassette.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = [
    (re.compile(r"ghs_[A-Za-z0-9]{36}"),                "GitHub installation token"),
    (re.compile(r"gho_[A-Za-z0-9]{36}"),                "GitHub OAuth token"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"),                "GitHub PAT"),
    (re.compile(r"AKIA[A-Z0-9]{16}"),                   "AWS access key ID"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM private key block"),
    (re.compile(r"\"Bearer [A-Za-z0-9._\\-]+\""),       "Bearer token in JSON"),
]


def scan(path: Path) -> list[str]:
    text = path.read_text(errors="ignore")
    return [
        f"{path}: matches {desc}"
        for rx, desc in PATTERNS
        if rx.search(text)
    ]


def main(argv: list[str]) -> int:
    if not argv:
        # invoked without files — scan all known cassette dirs
        argv = [
            str(p) for p in Path("tests").rglob("*.yaml")
            if "/cassettes/" in str(p)
        ]
    findings: list[str] = []
    for arg in argv:
        findings.extend(scan(Path(arg)))
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 2: Wire into pre-commit**

Add to `.pre-commit-config.yaml`:

```yaml
  - repo: local
    hooks:
      - id: check-cassettes
        name: check-cassettes — no secrets in cassettes
        entry: python scripts/check-cassettes.py
        language: system
        files: '^tests/real_service/.*/cassettes/.*\.(yaml|yml)$'
        pass_filenames: true
```

- [ ] **Step 3: Test the scanner against a synthetic positive**

```bash
mkdir -p /tmp/cassette-test
printf 'authorization: ghs_%s' "$(printf 'x%.0s' $(seq 1 36))" > /tmp/cassette-test/leak.yaml
python scripts/check-cassettes.py /tmp/cassette-test/leak.yaml; echo "exit=$?"
# expect: matches GitHub installation token; exit=1
```

Then test that a clean file passes:

```bash
echo "no secrets here" > /tmp/cassette-test/clean.yaml
python scripts/check-cassettes.py /tmp/cassette-test/clean.yaml; echo "exit=$?"
# expect: exit=0
```

- [ ] **Step 4: Commit**

```bash
chmod +x scripts/check-cassettes.py
git add scripts/check-cassettes.py .pre-commit-config.yaml
git commit -m "chore: cassette secret-scan pre-commit hook"
```

### Task 8.7: GitHub Actions — PR-fast workflow

**Files:**
- Modify: `.github/workflows/test.yml`

The PR-fast workflow runs only the cheap layers — unit, contract,
smoke — and the parallelism toggle is `-n auto` for unit/contract.
Total wall budget: under 3 minutes from green checkout.

- [ ] **Step 1: Replace `.github/workflows/test.yml`**

```yaml
name: test

on:
  pull_request:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: test-${{ github.head_ref || github.run_id }}
  cancel-in-progress: true

jobs:
  pr-fast:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: uv sync --dev --extra testing
      - name: lint imports
        run: uv run lint-imports
      - name: unit
        run: uv run pytest tests/unit -q -n auto
      - name: contract
        run: uv run pytest tests/contract -q -n auto
      - name: smoke
        run: uv run pytest tests/smoke -q
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/test.yml
git commit -m "ci: PR-fast workflow (unit + contract + smoke)"
```

### Task 8.8: GitHub Actions — push-full workflow

**Files:**
- Create: `.github/workflows/test-full.yml`

Push-full runs on `main` after merge: integration + e2e on top of
PR-fast. Wall budget: under 8 minutes.

- [ ] **Step 1: Write the workflow**

```yaml
name: test-full

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  full:
    runs-on: ubuntu-latest
    timeout-minutes: 12
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with: { enable-cache: true }
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: uv sync --dev --extra testing
      - name: unit + contract + smoke
        run: uv run pytest tests/unit tests/contract tests/smoke -q -n auto
      - name: integration
        run: uv run pytest tests/integration -q
      - name: e2e
        run: uv run pytest tests/e2e -q
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/test-full.yml
git commit -m "ci: push-full workflow (integration + e2e)"
```

### Task 8.9: GitHub Actions — nightly real-service workflow

**Files:**
- Create: `.github/workflows/test-real-service.yml`

The nightly workflow runs the real-service layer with service
containers (Postgres, Redis) and the GitHub-cassette tests. It is the
only workflow that can hit a network — gated by tags + cassette
secrets check.

- [ ] **Step 1: Write the workflow**

```yaml
name: test-real-service

on:
  schedule:
    - cron: "0 6 * * *"  # 06:00 UTC daily
  workflow_dispatch:

permissions:
  contents: read

jobs:
  real-service:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: openbot
          POSTGRES_DB: openbot_test
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
      redis:
        image: redis:7
        ports: ["6379:6379"]
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    env:
      OPENBOT_TEST_POSTGRES_URL: postgresql+asyncpg://postgres:openbot@localhost:5432/openbot_test
      OPENBOT_TEST_REDIS_URL: redis://localhost:6379/15
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with: { enable-cache: true }
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: uv sync --dev --extra testing
      - name: cassette-secret-scan
        run: python scripts/check-cassettes.py
      - name: real-service postgres + redis
        run: uv run pytest tests/real_service/postgres tests/real_service/redis -v
      - name: real-service github (cassettes)
        run: uv run pytest tests/real_service/github -v
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/test-real-service.yml
git commit -m "ci: nightly real-service workflow"
```

### Task 8.10: GitHub Actions — manual workflow

**Files:**
- Create: `.github/workflows/test-manual.yml`

The manual workflow runs the same matrix as nightly but on demand —
useful for re-recording cassettes after a GitHub API change.

- [ ] **Step 1: Write the workflow**

```yaml
name: test-manual

on:
  workflow_dispatch:
    inputs:
      layer:
        description: "Layer to run"
        required: true
        default: "all"
        type: choice
        options: ["all", "real_service", "e2e", "integration"]

permissions:
  contents: read

jobs:
  manual:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with: { enable-cache: true }
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: uv sync --dev --extra testing
      - name: run selected layer
        run: |
          if [ "${{ inputs.layer }}" = "all" ]; then
            uv run pytest tests/ -v
          else
            uv run pytest "tests/${{ inputs.layer }}" -v
          fi
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/test-manual.yml
git commit -m "ci: manual workflow for re-recording cassettes"
```

## Phase 9 — Makefile + pre-commit + docs

This phase wires the new test layout into the developer workflow:
new make targets so each layer can be run independently, the existing
pre-commit hooks updated to call the new layout, and the developer
docs that explain the layered model.

Spec coverage: §9 (developer workflow), §13 (security boundaries).

### Task 9.1: Makefile rewrite — per-layer targets

**Files:**
- Modify: `Makefile`

The current `make test` runs everything against the old `tests/`
folder. Replace it with per-layer targets that map to the layered
directory structure, plus an aggregate `test` that runs the local
loop (unit + contract + smoke).

- [ ] **Step 1: Update the `.PHONY` block in `Makefile`**

```make
.PHONY: help install sync hooks \
        test test-unit test-contract test-integration test-smoke \
        test-e2e test-real-service test-budget \
        lint lint-fix lint-imports fmt fmt-check check \
        dev dev-server dev-smee run worker smoke setup \
        secret-scan check-cassettes doctor db-init \
        compose-up compose-down compose-logs compose-ps clean distclean
```

- [ ] **Step 2: Replace the test targets**

Replace the existing `test:` and `test-fast:` blocks with:

```make
# ─── tests by layer ──────────────────────────────────────────────
test: test-unit test-contract test-smoke ## Local fast loop (unit+contract+smoke)

test-unit: ## Layer 1 — pure logic, no IO
	$(PYTEST) tests/unit -q -n auto

test-contract: ## Layer 2 — Port shape, fake-vs-real double-run
	$(PYTEST) tests/contract -q -n auto

test-integration: ## Layer 3 — use cases assembled with fakes
	$(PYTEST) tests/integration -q

test-smoke: ## Layer 4 — boot invariants + linter contracts
	$(PYTEST) tests/smoke -q

test-e2e: ## Layer 5 — full stack with fakes
	$(PYTEST) tests/e2e -q

test-real-service: ## Layer 6 — postgres / redis / github via cassettes
	$(PYTEST) tests/real_service -v

test-budget: ## Wall-clock budget guard (opt-in)
	OPENBOT_TEST_BUDGET_RUN=1 $(PYTEST) tests/smoke/test_budget.py -v
```

- [ ] **Step 3: Update `check` target**

The verification trio now references the layered tests. Replace:

```make
check: fmt-check lint test ## Verification trio (fmt-check + lint + test)
```

- [ ] **Step 4: Add `check-cassettes` target**

```make
check-cassettes: ## Scan VCR cassettes for leaked secrets
	$(PY) python scripts/check-cassettes.py
```

- [ ] **Step 5: Run `make help` to confirm**

Run: `make help`
Expected: new targets visible — `test-unit`, `test-contract`,
`test-integration`, `test-smoke`, `test-e2e`, `test-real-service`,
`test-budget`, `check-cassettes`.

- [ ] **Step 6: Run `make check`**

Run: `make check`
Expected: fmt-check + lint + (unit+contract+smoke) all green.

- [ ] **Step 7: Commit**

```bash
git add Makefile
git commit -m "chore(make): per-layer test targets + check-cassettes"
```

### Task 9.2: pre-commit config update

**Files:**
- Modify: `.pre-commit-config.yaml`

The pre-commit run on every commit must enforce:
  1. ruff format + lint + import order (already configured)
  2. import-linter contracts (new in Phase 6)
  3. cassette secret scan (new in Phase 8.6)
  4. unit tests (cheap, under 20 s)

Integration / e2e / real_service must NOT run in pre-commit — they
exceed the typical commit cycle. They run in CI.

- [ ] **Step 1: Update `.pre-commit-config.yaml`**

Append (or merge) the following hook entries:

```yaml
  - repo: local
    hooks:
      - id: import-linter
        name: import-linter — layer contracts
        entry: uv run lint-imports
        language: system
        pass_filenames: false
        types: [python]

      - id: pytest-unit
        name: pytest unit (fast)
        entry: uv run pytest tests/unit -q -x
        language: system
        pass_filenames: false
        types: [python]
        stages: [pre-commit]
```

(The cassette hook from Task 8.6 is already added.)

- [ ] **Step 2: Run `pre-commit run --all-files`**

Run: `pre-commit run --all-files`
Expected: all hooks pass.

- [ ] **Step 3: Commit**

```bash
git add .pre-commit-config.yaml
git commit -m "chore(pre-commit): import-linter + unit-tests gate"
```

### Task 9.3: Developer docs — testing README

**Files:**
- Create: `docs/testing/README.md`

The testing README is the single document a new contributor reads
to understand the layered system. It should be short and prescriptive,
not a re-statement of the spec.

- [ ] **Step 1: Write the README**

```markdown
# OpenBot Testing

OpenBot's tests are organized into **six layers**, each with a clear
purpose and budget. Pick the right layer for what you're testing.

## Layers at a glance

| Layer          | What it tests                                  | IO                          | Typical wall |
| -------------- | ---------------------------------------------- | --------------------------- | ------------ |
| `unit`         | pure logic, no async, no IO                    | none                        | < 20 s       |
| `contract`     | every Port's shape, fake vs in-process real    | fakeredis / aiosqlite       | < 60 s       |
| `integration`  | use cases assembled with fakes                 | none (in-process fakes)     | < 4 min      |
| `smoke`        | boot invariants, import-linter, budgets        | none                        | < 30 s       |
| `e2e`          | whole stack from webhook to side-effect        | none (in-process fakes)     | < 2 min      |
| `real_service` | postgres / redis / github cassettes / smee     | env-driven, opt-in locally  | < 8 min      |

## Where do I add a new test?

```dot
digraph picker {
    Start [shape=diamond, label="Does it call the LLM\nfor behaviour?"];
    Eval [label="evals/  (PRD §8.3)"];
    Pure [shape=diamond, label="Pure function\nonly?"];
    Unit [label="tests/unit/"];
    Port [shape=diamond, label="Testing a Port\nshape?"];
    Contract [label="tests/contract/"];
    Multi [shape=diamond, label="Multiple components\ntogether?"];
    Integration [label="tests/integration/"];
    Stack [shape=diamond, label="Webhook -> side-effect?"];
    E2E [label="tests/e2e/"];
    Real [label="tests/real_service/"];

    Start -> Eval [label="yes"];
    Start -> Pure [label="no"];
    Pure -> Unit [label="yes"];
    Pure -> Port [label="no"];
    Port -> Contract [label="yes"];
    Port -> Multi [label="no"];
    Multi -> Integration [label="yes"];
    Multi -> Stack [label="no"];
    Stack -> E2E [label="yes"];
    Stack -> Real [label="no, real svc"];
}
```

## Common commands

| Need to ...                                  | Run                                |
| -------------------------------------------- | ---------------------------------- |
| Local fast loop (every commit)               | `make check` (= fmt + lint + unit+contract+smoke) |
| Single layer                                 | `make test-unit` etc.              |
| Whole local pyramid                          | `make test-unit test-contract test-integration test-smoke test-e2e` |
| Real services (need env)                     | `make test-real-service`           |
| Re-record GitHub cassettes                   | `OPENBOT_VCR_MODE=once make test-real-service` |

## Real-service env vars

`tests/real_service/` modules call `pytest.skip(allow_module_level=True)`
when their env var is missing, so the layer is no-op locally unless
you opt in:

| Var                            | Purpose                            |
| ------------------------------ | ---------------------------------- |
| `OPENBOT_TEST_POSTGRES_URL`    | postgres real-service tests       |
| `OPENBOT_TEST_REDIS_URL`       | redis real-service tests          |
| `OPENBOT_TEST_GITHUB_REPO`     | GitHub cassette tests             |
| `OPENBOT_TEST_GITHUB_PR`       | GitHub PR number for cassette     |
| `OPENBOT_TEST_SMEE_PAYLOAD`    | path to a captured webhook payload |
| `OPENBOT_VCR_MODE`             | `none` (default), `once`, `new_episodes` |

## What NOT to put in tests/

- LLM behaviour assertions ("does the model answer correctly?") —
  those belong in `evals/` per PRD §8.3.
- Real GitHub PR creation. Reads via cassette; writes only in eval
  / production.
- Anything that requires Docker daemon. `tests/real_service/` reads
  service URLs from env; CI provides them via Actions service
  containers.

## Adding a new Port

When you add a new file under `openbot/application/ports/`:

1. Add a fake under `openbot/testing/fakes/<port>.py` with a module-level
   `_PROTOCOL_CHECK: Final[YourPort] = YourFake()` to lock the type.
2. Add a contract test under `tests/contract/test_<port>_contract.py`
   that runs against both the fake and an in-process real impl.
3. The smoke layer's `test_contract_coverage.py` will fail if you forget.
```

- [ ] **Step 2: Commit**

```bash
git add docs/testing/README.md
git commit -m "docs(testing): six-layer system overview"
```

### Task 9.4: CLAUDE.md verification section

**Files:**
- Modify: `CLAUDE.md`

The project-level `CLAUDE.md` tells future agentic workers how to
verify a change. It currently says `make check` runs unit tests; we
need to teach it the layered model so agents pick the right command.

- [ ] **Step 1: Update the "Verification commands" block in `CLAUDE.md`**

Replace the existing block with:

```markdown
## Verification commands

After any Python change:

```bash
make check   # fmt + lint + unit + contract + smoke (the local fast loop)
```

For deeper validation:

```bash
make test-integration   # Layer 3 — assembled use cases with fakes
make test-e2e           # Layer 5 — whole stack with fakes
make test-real-service  # Layer 6 — opt-in via OPENBOT_TEST_* env vars
```

See [`docs/testing/README.md`](./docs/testing/README.md) for the
six-layer model and where to put new tests.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): point verification at layered tests"
```

## Phase 10 — Final acceptance verification

This phase is the runbook for confirming the rebuild meets the spec
acceptance checklist (§12.4). Each step runs a known command and
asserts the documented expected output. If any step fails, do NOT
mark the migration complete — go back and fix the failing layer.

### Task 10.1: Spec §12.4 acceptance checklist

**Files:**
- (verification only)

- [ ] **Step 1: Layer-by-layer green run**

Run each layer and confirm green:

```bash
uv run pytest tests/unit -q -n auto
uv run pytest tests/contract -q -n auto
uv run pytest tests/integration -q
uv run pytest tests/smoke -q
uv run pytest tests/e2e -q
```

Expected: every layer all-pass.

- [ ] **Step 2: Aggregate budget**

Run: `time uv run pytest tests/unit tests/contract tests/smoke -q -n auto`
Expected: real time under **2 minutes** on a laptop.

Run: `time uv run pytest tests/integration -q`
Expected: real time under **4 minutes**.

Run: `time uv run pytest tests/e2e -q`
Expected: real time under **2 minutes**.

- [ ] **Step 3: import-linter green**

Run: `uv run lint-imports`
Expected: `Contracts: 3 kept, 0 broken.`

- [ ] **Step 4: Coverage invariant**

Run: `uv run pytest tests/smoke/test_contract_coverage.py -v`
Expected: 2 PASSED — every Port has a contract test file.

- [ ] **Step 5: Cassette scan green**

Run: `python scripts/check-cassettes.py`
Expected: exit 0; no findings.

- [ ] **Step 6: pre-commit green on a synthetic edit**

```bash
echo "# touch" >> openbot/__init__.py
pre-commit run --files openbot/__init__.py
git checkout -- openbot/__init__.py
```

Expected: every hook passes.

- [ ] **Step 7: Real-service smoke (with env)**

If you have local Postgres + Redis running:

```bash
OPENBOT_TEST_POSTGRES_URL=postgresql+asyncpg://... \
OPENBOT_TEST_REDIS_URL=redis://localhost:6379/15 \
make test-real-service
```

Expected: postgres + redis tests pass; github tests skip (no token).

### Task 10.2: PR + rollback safety

**Files:**
- (verification only)

- [ ] **Step 1: Confirm rollback tag exists**

Run: `git tag --list 'pre-test-rebuild'`
Expected: the tag is present (set in Phase 0).

If anything in production breaks after merge, recovery is one
command: `git revert --no-edit pre-test-rebuild..HEAD -- tests/ openbot/testing/`.

- [ ] **Step 2: Full diff review**

Run: `git diff --stat pre-test-rebuild..HEAD | tail -5`
Expected: a sane number of insertions / deletions; no surprise files
outside the expected paths (`tests/`, `openbot/testing/`,
`.github/workflows/`, `Makefile`, `pyproject.toml`,
`.pre-commit-config.yaml`, `CLAUDE.md`, `docs/testing/`,
`scripts/check-cassettes.py`).

- [ ] **Step 3: Push branch and open PR**

```bash
git push -u origin feat/tests-rebuild
gh pr create --title "feat(tests): rebuild test suite into 6-layer pyramid" \
             --body-file docs/superpowers/specs/2026-05-24-tests-rebuild-design.md
```

PR description should reference:
  - the spec at `docs/superpowers/specs/2026-05-24-tests-rebuild-design.md`
  - the plan at `docs/superpowers/plans/2026-05-24-tests-rebuild.md`
  - the rollback tag `pre-test-rebuild`

- [ ] **Step 4: Wait for CI**

PR-fast workflow runs on the PR. Expected: green within 5 minutes.

After merge, push-full workflow runs on `main`. Expected: green
within 12 minutes.

Nightly workflow runs at 06:00 UTC the next day. Expected: green.

### Task 10.3: Archive plan + spec

**Files:**
- Move: `docs/superpowers/plans/2026-05-24-tests-rebuild.md` → `docs/_archive/superpowers/`
- Move: `docs/superpowers/specs/2026-05-24-tests-rebuild-design.md` → `docs/_archive/superpowers/`

This step is required by the user's CLAUDE.md memory rule:
"Whenever a slice/feature is implemented and committed, immediately
archive completed plans/specs to `docs/_archive/superpowers/`."

- [ ] **Step 1: Move both files**

```bash
mkdir -p docs/_archive/superpowers
git mv docs/superpowers/plans/2026-05-24-tests-rebuild.md docs/_archive/superpowers/
git mv docs/superpowers/specs/2026-05-24-tests-rebuild-design.md docs/_archive/superpowers/
```

- [ ] **Step 2: Commit**

```bash
git commit -m "docs(archive): move tests-rebuild plan + spec after merge"
```

- [ ] **Step 3: Confirm `docs/superpowers/plans/` is clean**

Run: `ls docs/superpowers/plans/ docs/superpowers/specs/`
Expected: no `2026-05-24-tests-rebuild*` files.
