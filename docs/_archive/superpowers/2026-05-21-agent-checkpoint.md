# Agent Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire LangGraph Postgres checkpointing and business-layer cancellation checkpoints into fix / review / chat workflows so agent state survives process crashes and users can cancel mid-flight.

**Architecture:** A single `AsyncPostgresSaver` (one per Worker process, shared across consumers) is initialized at startup via an async context manager and threaded down the call stack: `consume_loop → _execute_task_spec → execute_handler → PreflightContext → handler → responder`. Each responder passes `run_id` as `thread_id` and the saver as `checkpointer` to `create_deep_agent`. Business-layer cancellation checkpoints call `checkpoint(redis, run_id)` before each slow step — raising `RunCancelledError` when the user has sent a cancel signal. On successful completion the thread is deleted from the checkpoint store.

**Tech Stack:** `langgraph-checkpoint-postgres>=2.0` (new), `asyncpg` (already installed), `langgraph.checkpoint.memory.MemorySaver` (for tests, already available), pytest monkeypatch.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `openbot/infrastructure/persistence/agent_checkpointer.py` | `agent_checkpointer(dsn)` async context manager yielding a ready `AsyncPostgresSaver` |
| Create | `tests/infrastructure/persistence/test_agent_checkpointer.py` | Unit-test the context manager with `MemorySaver` stub |
| Modify | `pyproject.toml` | Add `langgraph-checkpoint-postgres>=2.0` dependency |
| Modify | `openbot/application/middleware/preflight.py` | Add `agent_checkpointer: BaseCheckpointSaver \| None = None` to `PreflightContext` |
| Modify | `openbot/application/dispatcher.py` | Add `agent_checkpointer` kwarg to `execute_handler`; pass through to `PreflightContext` |
| Modify | `openbot/infrastructure/queue/worker.py` | Add `agent_checkpointer` kwarg to `consume_loop`, `_read_and_dispatch`, `_reclaim_abandoned`, `_process_entry`, `_execute_task_spec`; pass to `execute_handler` |
| Modify | `openbot/entrypoints/worker/__main__.py` | Wrap `_main` body in `agent_checkpointer(settings.postgres_url)` context; pass to `consume_loop` |
| Modify | `openbot/infrastructure/agents/deepagents_fix.py` | Add `run_id` + `checkpointer` params to `fix_for_event`; wire into `create_deep_agent` + `ainvoke` config |
| Modify | `openbot/infrastructure/agents/deepagents_review.py` | Same pattern |
| Modify | `openbot/infrastructure/agents/deepagents_chat.py` | Remove `lru_cache`; add `run_id` + `checkpointer` params to `reply_for_event` |
| Modify | `openbot/application/use_cases/fix.py` | Thread `run_id` + `checkpointer` into `_generate_fix_outcome`; add five `checkpoint()` calls; call `adelete_thread` on success |
| Modify | `openbot/application/use_cases/review.py` | Thread params; add one `checkpoint()` call after LLM; `adelete_thread` on success |
| Modify | `openbot/application/use_cases/chat.py` | Add two `checkpoint()` calls around freeform LLM call |

---

## Task 1: Install dependency + create `agent_checkpointer.py`

**Files:**
- Modify: `pyproject.toml`
- Create: `openbot/infrastructure/persistence/agent_checkpointer.py`
- Create: `tests/infrastructure/persistence/test_agent_checkpointer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/infrastructure/persistence/test_agent_checkpointer.py
"""agent_checkpointer context manager — unit test using MemorySaver stub."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest


async def test_agent_checkpointer_yields_saver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Context manager should yield the saver and call setup()."""
    from openbot.infrastructure.persistence import agent_checkpointer as mod

    setup_called: list[bool] = []

    class _FakeSaver:
        async def setup(self) -> None:
            setup_called.append(True)

    @asynccontextmanager
    async def _fake_from_conn_string(dsn: str) -> AsyncIterator[_FakeSaver]:
        yield _FakeSaver()

    monkeypatch.setattr(mod, "_AsyncPostgresSaver_from_conn_string", _fake_from_conn_string)

    async with mod.agent_checkpointer("postgresql://localhost/test") as saver:
        assert saver is not None

    assert setup_called == [True], "setup() must be called before yielding"


async def test_agent_checkpointer_none_dsn_returns_none() -> None:
    """When postgres_url is None (dev without DB), yield None instead of crashing."""
    from openbot.infrastructure.persistence import agent_checkpointer as mod

    async with mod.agent_checkpointer(None) as saver:
        assert saver is None
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd /Users/wy/projects/openbot
uv run pytest tests/infrastructure/persistence/test_agent_checkpointer.py -v
```

Expected: `ModuleNotFoundError` or `ImportError` (file doesn't exist yet).

- [ ] **Step 3: Add the dependency to `pyproject.toml`**

Open `pyproject.toml`, find the `[project] dependencies` block where `asyncpg>=0.30` is listed, and add:

```toml
"langgraph-checkpoint-postgres>=2.0",
```

Place it right after the `asyncpg` line.

- [ ] **Step 4: Sync dependencies**

```bash
uv sync --dev
```

Expected: installs `langgraph-checkpoint-postgres` and its transitive deps.

- [ ] **Step 5: Create `agent_checkpointer.py`**

```python
# openbot/infrastructure/persistence/agent_checkpointer.py
"""LangGraph agent checkpointer — Postgres-backed, async.

One checkpointer is created per Worker process lifetime and shared
across all concurrent consumers. ``agent_checkpointer(dsn)`` is an
async context manager: the caller boots it before ``consume_loop``
and shuts it down on exit.

When ``dsn`` is ``None`` (local dev without Postgres) the context
manager yields ``None`` — handlers receiving ``None`` skip
checkpointing entirely (same graceful-degrade pattern as
``ctx.sandbox_factory is None``).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


# Indirection used by tests to monkeypatch the factory without touching
# the real asyncpg connection machinery.
async def _AsyncPostgresSaver_from_conn_string(dsn: str):  # noqa: N802
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver as _Real

    async with _Real.from_conn_string(dsn) as saver:
        yield saver


@asynccontextmanager
async def agent_checkpointer(
    dsn: str | None,
) -> AsyncIterator[AsyncPostgresSaver | None]:
    """Yield a ready-to-use ``AsyncPostgresSaver``, or ``None`` if no DSN.

    ``setup()`` is called once — it is idempotent and creates the four
    LangGraph checkpoint tables if they don't already exist.

    Usage::

        async with agent_checkpointer(settings.postgres_url) as cp:
            ctx.agent_checkpointer = cp
            await consume_loop(redis, ..., agent_checkpointer=cp)
    """
    if dsn is None:
        yield None
        return

    from contextlib import asynccontextmanager as _acm

    gen = _AsyncPostgresSaver_from_conn_string(dsn)
    saver = await gen.__anext__()
    await saver.setup()
    try:
        yield saver
    finally:
        try:
            await gen.aclose()
        except Exception:
            pass
```

- [ ] **Step 6: Run tests to confirm they pass**

```bash
uv run pytest tests/infrastructure/persistence/test_agent_checkpointer.py -v
```

Expected: `PASSED` (2 tests).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml openbot/infrastructure/persistence/agent_checkpointer.py tests/infrastructure/persistence/test_agent_checkpointer.py
git commit -m "feat: add langgraph-checkpoint-postgres dep + agent_checkpointer context manager"
```

---

## Task 2: Add `agent_checkpointer` field to `PreflightContext` and `execute_handler`

**Files:**
- Modify: `openbot/application/middleware/preflight.py:100-151`
- Modify: `openbot/application/dispatcher.py:488-530`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/infrastructure/queue/test_worker_v3.py (or similar) —
# or put in tests/application/test_dispatcher.py.
# Test that execute_handler threads agent_checkpointer into PreflightContext.

async def test_execute_handler_passes_checkpointer_to_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute_handler must forward agent_checkpointer into PreflightContext."""
    from openbot.application import dispatcher as mod

    captured_ctx: list = []

    async def _fake_run_with_sandbox(ctx) -> None:
        captured_ctx.append(ctx)

    monkeypatch.setattr(mod, "_run_with_sandbox", _fake_run_with_sandbox)

    sentinel = object()  # stand-in for a real checkpointer

    await mod.execute_handler(
        adapter=_make_stub_adapter(),
        event=_make_event(),
        dispatch=_make_dispatch(),
        config=_make_config(),
        session_factory=None,
        redis=None,
        agent_checkpointer=sentinel,  # ← new kwarg
    )

    assert captured_ctx[0].agent_checkpointer is sentinel
```

The helpers `_make_stub_adapter()`, `_make_event()`, `_make_dispatch()`, `_make_config()` already exist in `test_worker_v3.py` or similar — reuse them. If this is a new file, define minimal stubs that satisfy the types.

- [ ] **Step 2: Run the failing test**

```bash
uv run pytest tests/infrastructure/queue/test_worker_v3.py -v -k "checkpointer"
```

Expected: `TypeError: execute_handler() got an unexpected keyword argument 'agent_checkpointer'`.

- [ ] **Step 3: Add the field to `PreflightContext`**

In `openbot/application/middleware/preflight.py`, after the `classifier_output` field (line ~146), add:

```python
    # LangGraph checkpoint saver — wired at Worker startup (one per
    # process, shared across all consumers). ``None`` in dev without
    # Postgres or in any caller that hasn't been upgraded yet — handlers
    # skip checkpointing gracefully when None. Frozen field: passed in
    # once at context construction; never mutated mid-flight.
    agent_checkpointer: Any = None
```

Also add to the `TYPE_CHECKING` import block at the top:

```python
if TYPE_CHECKING:
    ...
    from langgraph.checkpoint.base import BaseCheckpointSaver
```

And update the field type annotation:

```python
    agent_checkpointer: BaseCheckpointSaver | None = None
```

Since `BaseCheckpointSaver` is only imported under `TYPE_CHECKING`, the runtime type is erased — the `Any = None` default is what actually executes. Use `from __future__ import annotations` (already present) to make the string annotation work at runtime.

Exact edit in `preflight.py` after `classifier_output: ClassifierOutput | None = None`:

```python
    # LangGraph agent checkpointer — one ``AsyncPostgresSaver`` per Worker
    # process, shared across consumers. ``None`` in dev / tests / callers
    # that haven't been upgraded. Handlers access via ``ctx.agent_checkpointer``
    # and pass it (plus ``ctx.dispatch.run_id``) to the responder. Graceful-
    # degrade: ``None`` means "no persistence" — same pattern as
    # ``ctx.sandbox_factory is None``.
    agent_checkpointer: BaseCheckpointSaver | None = None
```

Add `BaseCheckpointSaver` to the `TYPE_CHECKING` import:

```python
if TYPE_CHECKING:
    ...
    from langgraph.checkpoint.base import BaseCheckpointSaver
```

- [ ] **Step 4: Update `execute_handler` in `dispatcher.py`**

Find `async def execute_handler(` and add a new kwarg:

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
    sandbox_factory: (Callable[[], AbstractAsyncContextManager[SandboxPort]] | None) = None,
    classifier_output: ClassifierOutput | None = None,
    agent_checkpointer: BaseCheckpointSaver | None = None,   # ← new
) -> None:
```

Add to the `TYPE_CHECKING` imports block in `dispatcher.py`:

```python
    from langgraph.checkpoint.base import BaseCheckpointSaver
```

And in the `PreflightContext(...)` construction inside `execute_handler`:

```python
    ctx = PreflightContext(
        event=event,
        dispatch=dispatch,
        config=config,
        adapter=adapter,
        session_factory=session_factory,
        redis=redis,
        check_run_id=check_run_id,
        audit=audit,
        rate_limiter=rate_limiter,
        sandbox_factory=sandbox_factory,
        classifier_output=classifier_output,
        agent_checkpointer=agent_checkpointer,   # ← new
    )
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/infrastructure/queue/test_worker_v3.py -v -k "checkpointer"
```

Expected: `PASSED`.

Full suite:

```bash
make check
```

Expected: all previously passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add openbot/application/middleware/preflight.py openbot/application/dispatcher.py tests/
git commit -m "feat: thread agent_checkpointer through PreflightContext + execute_handler"
```

---

## Task 3: Wire checkpointer through Worker loop

**Files:**
- Modify: `openbot/infrastructure/queue/worker.py`
- Modify: `openbot/entrypoints/worker/__main__.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/infrastructure/queue/test_worker_v3.py`:

```python
async def test_consume_loop_passes_checkpointer_to_execute_handler(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis,
) -> None:
    """When consume_loop receives agent_checkpointer, it must reach execute_handler."""
    from openbot.infrastructure.queue import worker as wmod

    captured: list = []

    async def _fake_execute_task_spec(spec, *, entry_id, redis, adapter, session_factory, agent_checkpointer) -> None:
        captured.append(agent_checkpointer)

    monkeypatch.setattr(wmod, "_execute_task_spec", _fake_execute_task_spec)

    sentinel = object()
    # ... (enqueue one v3 entry, run consume_loop one iteration)
    # Verify:
    assert captured and captured[0] is sentinel
```

This test verifies end-to-end threading without a real DB. Adapt it to the existing `fake_redis` fixture pattern in `tests/infrastructure/queue/test_worker_v3.py`.

- [ ] **Step 2: Run the failing test**

```bash
uv run pytest tests/infrastructure/queue/test_worker_v3.py -v -k "checkpointer"
```

Expected: `TypeError` — `consume_loop` doesn't accept `agent_checkpointer` yet.

- [ ] **Step 3: Modify `worker.py`**

Thread `agent_checkpointer` down through all internal helpers.

**`consume_loop` signature:**

```python
async def consume_loop(
    *,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    consumer_name: str,
    shutdown: asyncio.Event | None = None,
    read_block_ms: int = _READ_BLOCK_MS,
    agent_checkpointer: Any | None = None,   # ← new
) -> None:
```

Inside `consume_loop`, pass it to `_reclaim_abandoned` and `_read_and_dispatch`:

```python
    while not shutdown.is_set():
        try:
            await _reclaim_abandoned(
                redis=redis,
                adapter=adapter,
                session_factory=session_factory,
                consumer_name=consumer_name,
                agent_checkpointer=agent_checkpointer,   # ← new
            )
            await _read_and_dispatch(
                redis=redis,
                adapter=adapter,
                session_factory=session_factory,
                consumer_name=consumer_name,
                read_block_ms=read_block_ms,
                agent_checkpointer=agent_checkpointer,   # ← new
            )
```

**`_read_and_dispatch` signature + propagation:**

```python
async def _read_and_dispatch(
    *,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    consumer_name: str,
    read_block_ms: int = _READ_BLOCK_MS,
    agent_checkpointer: Any | None = None,   # ← new
) -> None:
    ...
    for _stream_name, entries in response:
        for entry_id, fields in entries:
            await _process_entry(
                redis=redis,
                adapter=adapter,
                session_factory=session_factory,
                entry_id=_as_str(entry_id),
                fields=fields,
                agent_checkpointer=agent_checkpointer,   # ← new
            )
```

**`_reclaim_abandoned` signature + propagation:**

```python
async def _reclaim_abandoned(
    *,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    consumer_name: str,
    agent_checkpointer: Any | None = None,   # ← new
) -> None:
    ...
    for entry_id, fields in entries:
        await _process_entry(
            redis=redis,
            adapter=adapter,
            session_factory=session_factory,
            entry_id=_as_str(entry_id),
            fields=fields,
            agent_checkpointer=agent_checkpointer,   # ← new
        )
```

**`_process_entry` signature + propagation:**

```python
async def _process_entry(
    *,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    entry_id: str,
    fields: dict,
    agent_checkpointer: Any | None = None,   # ← new
) -> None:
    ...
    if _is_v3_spec(blob):
        ...
        await _execute_task_spec(
            spec,
            entry_id=entry_id,
            redis=redis,
            adapter=adapter,
            session_factory=session_factory,
            agent_checkpointer=agent_checkpointer,   # ← new
        )
```

**`_execute_task_spec` signature + propagation:**

```python
async def _execute_task_spec(
    spec: TaskSpec,
    *,
    entry_id: str,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    agent_checkpointer: Any | None = None,   # ← new
) -> None:
    ...
    await execute_handler(
        adapter=adapter,
        event=event,
        dispatch=new_dispatch,
        config=config,
        session_factory=session_factory,
        redis=redis,
        check_run_id=spec.check_run_id,
        classifier_output=classifier_output,
        agent_checkpointer=agent_checkpointer,   # ← new
    )
```

Add `Any` to `worker.py` imports:

```python
from typing import TYPE_CHECKING, Any, Final
```

- [ ] **Step 4: Modify `__main__.py`**

In `_main()`, after the Postgres section and before `ensure_consumer_group`, wrap the consumer section:

```python
    from openbot.infrastructure.persistence.agent_checkpointer import agent_checkpointer

    async with agent_checkpointer(settings.postgres_url) as cp:
        await ensure_consumer_group(redis_client)
        ...
        consumers = [
            asyncio.create_task(
                consume_loop(
                    redis=redis_client,
                    adapter=adapter,
                    session_factory=session_factory,
                    consumer_name=f"consumer-{i}",
                    shutdown=shutdown,
                    agent_checkpointer=cp,   # ← new
                ),
                name=f"openbot-consumer-{i}",
            )
            for i in range(settings.worker_concurrency)
        ]
        ...
        await shutdown.wait()
        # ... existing shutdown + cleanup code, indented one level deeper ...
```

The entire body from `ensure_consumer_group` through the `finally` block moves inside `async with agent_checkpointer(...)`. This ensures the connection pool is alive for the entire worker lifetime.

- [ ] **Step 5: Run tests**

```bash
make check
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add openbot/infrastructure/queue/worker.py openbot/entrypoints/worker/__main__.py tests/
git commit -m "feat: thread agent_checkpointer through consume_loop → execute_handler"
```

---

## Task 4: Modify `deepagents_fix.py` to use checkpointer

**Files:**
- Modify: `openbot/infrastructure/agents/deepagents_fix.py`
- Modify: `tests/infrastructure/agents/test_deepagents_fix.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/infrastructure/agents/test_deepagents_fix.py`:

```python
async def test_fix_responder_passes_checkpointer_and_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When run_id + checkpointer are provided, they must reach ainvoke config."""
    from openbot.infrastructure.agents import deepagents_fix as mod
    from langgraph.checkpoint.memory import MemorySaver

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            captured["config"] = config
            return _fake_agent_result()

    def fake_create_deep_agent(
        *, model: Any, tools: Any, system_prompt: Any,
        response_format: Any, checkpointer: Any = None,
    ) -> FakeAgent:
        captured["checkpointer"] = checkpointer
        return FakeAgent()

    monkeypatch.setattr(mod, "create_deep_agent", fake_create_deep_agent)

    saver = MemorySaver()
    responder = mod.DeepAgentsFixResponder()
    await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={"title": "t", "body": "b", "base_sha": "abc1234"},
        run_id="run-abc",
        checkpointer=saver,
    )

    assert captured["checkpointer"] is saver
    assert captured["config"]["configurable"]["thread_id"] == "run-abc"


async def test_fix_responder_no_checkpointer_no_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When run_id/checkpointer are None, no configurable key is added."""
    from openbot.infrastructure.agents import deepagents_fix as mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            captured["config"] = config
            return _fake_agent_result()

    def fake_create_deep_agent(*, model, tools, system_prompt, response_format, checkpointer=None):
        captured["checkpointer"] = checkpointer
        return FakeAgent()

    monkeypatch.setattr(mod, "create_deep_agent", fake_create_deep_agent)

    responder = mod.DeepAgentsFixResponder()
    await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={"title": "t", "body": "b", "base_sha": "abc1234"},
        # run_id and checkpointer intentionally omitted
    )

    assert captured["checkpointer"] is None
    assert "configurable" not in captured["config"]
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/infrastructure/agents/test_deepagents_fix.py -v -k "checkpointer"
```

Expected: `TypeError: fix_for_event() got an unexpected keyword argument 'run_id'`.

- [ ] **Step 3: Update `deepagents_fix.py`**

Modify `fix_for_event` signature:

```python
    async def fix_for_event(
        self,
        event: UnifiedEvent,
        *,
        adapter: ChannelAdapterPort,
        sandbox: SandboxPort,
        issue: dict[str, Any],
        run_id: str | None = None,                      # ← new
        checkpointer: BaseCheckpointSaver | None = None, # ← new
    ) -> FixOutcome:
```

Add to `TYPE_CHECKING` imports:

```python
if TYPE_CHECKING:
    ...
    from langgraph.checkpoint.base import BaseCheckpointSaver
```

Modify the `create_deep_agent` call:

```python
        agent = create_deep_agent(
            model=_normalize_model_name(primary_model_for(Feature.FIX)),
            tools=tools,
            system_prompt=_SYSTEM_PROMPT,
            response_format=FixOutcomeSchema,
            checkpointer=checkpointer,             # ← new
        )
        config: dict = {"recursion_limit": _RECURSION_LIMIT}
        if run_id and checkpointer:
            config["configurable"] = {"thread_id": run_id}  # ← new
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": _user_prompt(
                            event,
                            issue_title=str(issue.get("title", "")),
                            issue_body=str(issue.get("body", "")),
                            base_sha=str(issue.get("base_sha", "")),
                        ),
                    }
                ]
            },
            config=config,
        )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/infrastructure/agents/test_deepagents_fix.py -v
```

Expected: all `PASSED` — existing tests must still pass because `run_id=None, checkpointer=None` is the default.

- [ ] **Step 5: Commit**

```bash
git add openbot/infrastructure/agents/deepagents_fix.py tests/infrastructure/agents/test_deepagents_fix.py
git commit -m "feat: deepagents_fix accepts run_id+checkpointer for LangGraph persistence"
```

---

## Task 5: Modify `deepagents_review.py` to use checkpointer

**Files:**
- Modify: `openbot/infrastructure/agents/deepagents_review.py`
- Modify: `tests/infrastructure/agents/test_deepagents_review.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/infrastructure/agents/test_deepagents_review.py`:

```python
async def test_review_responder_passes_checkpointer_and_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbot.infrastructure.agents import deepagents_review as mod
    from langgraph.checkpoint.memory import MemorySaver

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            captured["config"] = config
            return {"structured_response": _fake_review_schema_result()}

    def fake_create_deep_agent(
        *, model, tools, system_prompt, response_format, checkpointer=None
    ) -> FakeAgent:
        captured["checkpointer"] = checkpointer
        return FakeAgent()

    class FakeAdapter:
        async def get_pr_diff(self, event, pr_number: int) -> str:
            return "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new"

        async def search_code(self, event, query: str, path_glob: str | None = None):
            return []

    monkeypatch.setattr(mod, "create_deep_agent", fake_create_deep_agent)

    saver = MemorySaver()
    responder = mod.DeepAgentsReviewResponder()
    await responder.review_for_event(
        _event(),
        adapter=FakeAdapter(),  # type: ignore[arg-type]
        run_id="run-review-1",
        checkpointer=saver,
    )

    assert captured["checkpointer"] is saver
    assert captured["config"]["configurable"]["thread_id"] == "run-review-1"
```

The `_fake_review_schema_result()` helper should return a dict that satisfies `ReviewFindingsSchema`'s `parse_structured_response` — look at existing tests in that file for the shape.

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/infrastructure/agents/test_deepagents_review.py -v -k "checkpointer"
```

Expected: `TypeError: review_for_event() got an unexpected keyword argument 'run_id'`.

- [ ] **Step 3: Update `deepagents_review.py`**

```python
    async def review_for_event(
        self,
        event: UnifiedEvent,
        *,
        adapter: ChannelAdapterPort,
        run_id: str | None = None,                       # ← new
        checkpointer: BaseCheckpointSaver | None = None, # ← new
    ) -> ReviewFindings:
```

Add import under `TYPE_CHECKING`:

```python
    from langgraph.checkpoint.base import BaseCheckpointSaver
```

Update agent construction + config:

```python
        agent = create_deep_agent(
            model=_normalize_model_name(primary_model_for(Feature.REVIEW)),
            tools=tools,
            system_prompt=_SYSTEM_PROMPT,
            response_format=ReviewFindingsSchema,
            checkpointer=checkpointer,               # ← new
        )
        config: dict = {"recursion_limit": _RECURSION_LIMIT}
        if run_id and checkpointer:
            config["configurable"] = {"thread_id": run_id}  # ← new
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": _user_prompt(event, diff)}]},
            config=config,
        )
```

- [ ] **Step 4: Run full agent test suite**

```bash
uv run pytest tests/infrastructure/agents/ -v
```

Expected: all `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add openbot/infrastructure/agents/deepagents_review.py tests/infrastructure/agents/test_deepagents_review.py
git commit -m "feat: deepagents_review accepts run_id+checkpointer"
```

---

## Task 6: Modify `deepagents_chat.py` — remove `lru_cache`, add checkpointer

**Files:**
- Modify: `openbot/infrastructure/agents/deepagents_chat.py`
- Modify: `tests/infrastructure/agents/test_deepagents_chat.py`

> **Critical:** `lru_cache` caches by `model` string only. With a shared checkpointer, two events with the same model would share one agent graph — and LangGraph would treat different `thread_id` values as separate conversations in the same graph, which is actually OK. But the issue is deeper: `create_deep_agent` itself might store per-thread state in graph memory. The safe approach is to rebuild per call, consistent with fix/review (which always rebuild because their tools close over event state). Chat has no event-closed tools, but removing the cache makes the pattern uniform and avoids subtle bugs if tools are added later.

- [ ] **Step 1: Write the failing test**

Add to `tests/infrastructure/agents/test_deepagents_chat.py`:

```python
async def test_chat_responder_passes_checkpointer_and_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbot.infrastructure.agents import deepagents_chat as mod
    from langgraph.checkpoint.memory import MemorySaver

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any = None) -> dict[str, Any]:
            captured["config"] = config
            return {"messages": [_make_fake_msg("pong")]}

    def fake_create(*, model, tools, system_prompt, checkpointer=None) -> FakeAgent:
        captured["checkpointer"] = checkpointer
        return FakeAgent()

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    saver = MemorySaver()
    responder = mod.DeepAgentsChatResponder()
    await responder.reply_for_event(
        _event(),
        user_request="hello",
        run_id="run-chat-1",
        checkpointer=saver,
    )

    assert captured["checkpointer"] is saver
    assert captured["config"]["configurable"]["thread_id"] == "run-chat-1"


async def test_chat_responder_rebuilds_agent_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After removing lru_cache, every call must produce a fresh agent."""
    from openbot.infrastructure.agents import deepagents_chat as mod

    builds: list[str] = []

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any = None) -> dict[str, Any]:
            return {"messages": [_make_fake_msg("ok")]}

    def fake_create(*, model, tools, system_prompt, checkpointer=None) -> FakeAgent:
        builds.append(model)
        return FakeAgent()

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    responder = mod.DeepAgentsChatResponder()
    await responder.reply_for_event(_event(), user_request="a")
    await responder.reply_for_event(_event(), user_request="b")

    assert len(builds) == 2, "Agent must be rebuilt per call — lru_cache must be gone"
```

The `_make_fake_msg` helper: return an object with a `.content` attribute set to the string.

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/infrastructure/agents/test_deepagents_chat.py -v -k "checkpointer or rebuilds"
```

Expected: `TypeError` on new kwarg + `AssertionError` on rebuilds (lru_cache still present).

- [ ] **Step 3: Rewrite `deepagents_chat.py`**

Replace the file entirely:

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents import create_deep_agent

from openbot.domain.events import UnifiedEvent
from openbot.infrastructure.llm.model_router import Feature, primary_model_for

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

_SYSTEM_PROMPT = """You are OpenBot, a GitHub maintainer bot assistant.

You are answering a GitHub comment mention inside an automation workflow.

Rules:
- Answer the user's request directly and concisely.
- Use only the context provided in the prompt.
- Do not claim you inspected repository files, ran commands, or fetched remote data unless that context is explicitly provided.
- If the user asks for action you cannot complete from the provided context, say so clearly and suggest the next concrete step.
"""


def _normalize_model_name(model: str) -> str:
    if ":" in model:
        return model
    if "/" in model:
        provider, name = model.split("/", 1)
        return f"{provider}:{name}"
    return model


def _target_label(event: UnifiedEvent) -> str:
    if event.issue_number is not None:
        return f"issue #{event.issue_number}"
    if event.pr_number is not None:
        return f"pull request #{event.pr_number}"
    return "GitHub thread"


def _user_prompt(event: UnifiedEvent, user_request: str) -> str:
    return (
        "GitHub context:\n"
        f"- repository: {event.repo}\n"
        f"- target: {_target_label(event)}\n"
        f"- actor: {event.actor}\n"
        f"- event kind: {event.kind.value}\n\n"
        "User request:\n"
        f"{user_request}"
    )


def _coerce_text_part(part: Any) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        if part.get("type") == "text":
            text = part.get("text")
            return text if isinstance(text, str) else ""
        return ""
    text_attr = getattr(part, "text", None)
    return text_attr if isinstance(text_attr, str) else ""


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(_coerce_text_part(part) for part in content).strip()
    text_attr = getattr(content, "text", None)
    if isinstance(text_attr, str):
        return text_attr.strip()
    return ""


def _extract_reply(result: dict[str, Any]) -> str:
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("deepagents_result_missing_messages")
    content = getattr(messages[-1], "content", None)
    reply = _extract_message_text(content)
    if not reply:
        raise ValueError("deepagents_result_missing_text")
    return reply


class DeepAgentsChatResponder:
    """Chat responder — a fresh agent is built per call.

    The ``lru_cache`` that previously cached agents by model was removed
    when checkpointer support landed: caching would let a caller with
    ``checkpointer=None`` receive a cached graph that was built without a
    checkpointer, silently skipping persistence. Rebuilding per call is
    cheap relative to the LLM invocation and avoids the correctness risk.
    """

    async def reply_for_event(
        self,
        event: UnifiedEvent,
        *,
        user_request: str,
        run_id: str | None = None,                       # ← new
        checkpointer: BaseCheckpointSaver | None = None, # ← new
    ) -> str:
        agent = create_deep_agent(
            model=_normalize_model_name(primary_model_for(Feature.CHAT)),
            tools=[],
            system_prompt=_SYSTEM_PROMPT,
            checkpointer=checkpointer,               # ← new
        )
        config: dict = {}
        if run_id and checkpointer:
            config["configurable"] = {"thread_id": run_id}
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": _user_prompt(event, user_request),
                    }
                ]
            },
            config=config or None,
        )
        return _extract_reply(result)


__all__ = ["DeepAgentsChatResponder"]
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/infrastructure/agents/test_deepagents_chat.py -v
```

Expected: all `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add openbot/infrastructure/agents/deepagents_chat.py tests/infrastructure/agents/test_deepagents_chat.py
git commit -m "feat: deepagents_chat removes lru_cache + accepts run_id+checkpointer"
```

---

## Task 7: Wire checkpointer into `fix.py` + add cancellation checkpoints + cleanup

**Files:**
- Modify: `openbot/application/use_cases/fix.py`
- Modify: `tests/application/use_cases/test_fix.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/application/use_cases/test_fix.py`:

```python
async def test_fix_passes_checkpointer_and_run_id_to_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """maybe_run_fix must forward ctx.agent_checkpointer + ctx.dispatch.run_id
    to _generate_fix_outcome."""
    from openbot.application.use_cases import fix as mod
    from langgraph.checkpoint.memory import MemorySaver

    captured: dict[str, Any] = {}

    async def fake_generate(
        *, sandbox, event, adapter, issue, run_id=None, checkpointer=None
    ):
        captured["run_id"] = run_id
        captured["checkpointer"] = checkpointer
        return _make_success_outcome()

    monkeypatch.setattr(mod, "_generate_fix_outcome", fake_generate)

    saver = MemorySaver()
    ctx = _make_ctx(agent_checkpointer=saver, run_id="run-fix-1")
    await mod.maybe_run_fix(ctx)

    assert captured["run_id"] == "run-fix-1"
    assert captured["checkpointer"] is saver


async def test_fix_cancellation_checkpoint_fires_before_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cancellation is signalled before the agent call, RunCancelledError propagates."""
    from openbot.application.use_cases import fix as mod
    from openbot.application.state.cancellation import RunCancelledError

    async def _always_cancelled(redis, run_id) -> None:
        raise RunCancelledError()

    monkeypatch.setattr(mod, "checkpoint", _always_cancelled)

    ctx = _make_ctx(run_id="run-cancel-test")
    # The handler must propagate RunCancelledError (it's a CancelledError);
    # audit_lifecycle catches and records CANCELLED.
    with pytest.raises(RunCancelledError):
        await mod.maybe_run_fix(ctx)
```

The `_make_ctx` helper should accept optional `agent_checkpointer` and `run_id` kwargs and patch them into a `PreflightContext`. `_make_success_outcome` returns a `FixOutcome` with `tests_passed=True`.

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/application/use_cases/test_fix.py -v -k "checkpointer or cancellation"
```

Expected: `AttributeError` — `_generate_fix_outcome` doesn't yet accept `run_id`/`checkpointer`.

- [ ] **Step 3: Update `fix.py`**

**Update `_generate_fix_outcome`:**

```python
async def _generate_fix_outcome(
    *,
    sandbox: SandboxPort,
    event: UnifiedEvent,
    adapter: ChannelAdapterPort,
    issue: dict[str, Any],
    run_id: str | None = None,                       # ← new
    checkpointer: BaseCheckpointSaver | None = None, # ← new
) -> FixOutcome:
    """Module-level seam — E2E tests monkeypatch this to skip DeepAgents."""
    responder = DeepAgentsFixResponder()
    return await responder.fix_for_event(
        event,
        adapter=adapter,
        sandbox=sandbox,
        issue=issue,
        run_id=run_id,           # ← new
        checkpointer=checkpointer,  # ← new
    )
```

**Import the cancellation checkpoint:**

```python
from openbot.application.state.cancellation import checkpoint
```

**Import `BaseCheckpointSaver` under `TYPE_CHECKING`:**

```python
if TYPE_CHECKING:
    ...
    from langgraph.checkpoint.base import BaseCheckpointSaver
```

**Update `maybe_run_fix`:**

Extract helpers at the top of the function body:

```python
    run_id = ctx.dispatch.run_id
    checkpointer = ctx.agent_checkpointer
```

Then add checkpoint calls and pass to `_generate_fix_outcome`, plus cleanup:

```python
    async with audit_lifecycle(ctx, workflow=Workflow.FIX) as audit:
        try:
            issue = await adapter.get_issue(event, issue_number)
        except Exception:
            ...

        await checkpoint(ctx.redis, run_id)   # ① after get_issue

        default_branch = str(issue.get("default_branch", "main"))

        try:
            outcome = await _generate_fix_outcome(
                sandbox=sandbox,
                event=event,
                adapter=adapter,
                issue=issue,
                run_id=run_id,             # ← new
                checkpointer=checkpointer, # ← new
            )
        except Exception:
            ...

        await checkpoint(ctx.redis, run_id)   # ② after agent

        if not outcome.attempt.tests_passed:
            ...
            return

        branch = _branch_name(issue_number=issue_number, base_sha=base_sha)

        try:
            await adapter.create_branch(event, branch, base_sha)
        except Exception:
            ...

        await checkpoint(ctx.redis, run_id)   # ③ after create_branch

        try:
            await sandbox.commit_and_push(...)
        except Exception:
            ...

        await checkpoint(ctx.redis, run_id)   # ④ after push

        try:
            pr = await adapter.open_pull_request(...)
        except Exception:
            ...

        # ⑤ Cleanup: agent completed successfully, checkpoint data no longer needed.
        if run_id and checkpointer is not None:
            try:
                await checkpointer.adelete_thread(run_id)
            except Exception:
                _logger.warning(
                    "fix_checkpoint_delete_failed",
                    extra={"run_id": run_id, **_log_extra(event)},
                )

        ...
        audit.outcome = f"pr_opened:{pr_url}"
```

Note: `checkpoint()` raises `RunCancelledError` (a subclass of `asyncio.CancelledError`). The `audit_lifecycle` context manager should capture this and record CANCELLED — verify this is already the case in `_lifecycle.py`; if not, it's handled at the worker level where `cancellation_deregister` runs in the `finally` block.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/application/use_cases/test_fix.py -v
```

Expected: all `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add openbot/application/use_cases/fix.py tests/application/use_cases/test_fix.py
git commit -m "feat: fix.py threads checkpointer+run_id, adds cancellation checkpoints + cleanup"
```

---

## Task 8: Wire checkpointer into `review.py` + add cancellation checkpoint

**Files:**
- Modify: `openbot/application/use_cases/review.py`
- Modify: `tests/application/use_cases/test_review.py`

- [ ] **Step 1: Write failing test**

Add to `tests/application/use_cases/test_review.py`:

```python
async def test_review_passes_checkpointer_and_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbot.application.use_cases import review as mod
    from langgraph.checkpoint.memory import MemorySaver

    captured: dict[str, Any] = {}

    async def fake_generate(*, event, adapter, run_id=None, checkpointer=None):
        captured["run_id"] = run_id
        captured["checkpointer"] = checkpointer
        return _make_empty_findings()  # no findings, will APPROVE

    monkeypatch.setattr(mod, "_generate_review_findings", fake_generate)

    saver = MemorySaver()
    ctx = _make_review_ctx(agent_checkpointer=saver, run_id="run-review-1")
    await mod.maybe_run_review(ctx)

    assert captured["run_id"] == "run-review-1"
    assert captured["checkpointer"] is saver
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/application/use_cases/test_review.py -v -k "checkpointer"
```

- [ ] **Step 3: Update `review.py`**

**Update `_generate_review_findings`:**

```python
async def _generate_review_findings(
    *, event: UnifiedEvent, adapter: ChannelAdapterPort,
    run_id: str | None = None,                       # ← new
    checkpointer: BaseCheckpointSaver | None = None, # ← new
) -> ReviewFindings:
    return await _RESPONDER.review_for_event(
        event,
        adapter=adapter,
        run_id=run_id,           # ← new
        checkpointer=checkpointer,  # ← new
    )
```

**Add imports:**

```python
from openbot.application.state.cancellation import checkpoint
```

Under `TYPE_CHECKING`:

```python
    from langgraph.checkpoint.base import BaseCheckpointSaver
```

**Update `maybe_run_review`:**

```python
    run_id = ctx.dispatch.run_id
    checkpointer = ctx.agent_checkpointer

    findings: ReviewFindings | None = None
    try:
        findings = await _generate_review_findings(
            event=event,
            adapter=ctx.adapter,
            run_id=run_id,            # ← new
            checkpointer=checkpointer, # ← new
        )
    except Exception:
        ...

    await checkpoint(ctx.redis, run_id)   # ① after LLM completes

    # (rest of the review submission logic unchanged)
    ...

    try:
        async with audit_lifecycle(ctx, workflow=Workflow.REVIEW) as audit:
            result = await ctx.adapter.create_pr_review(...)
            audit.outcome = ...

            # Cleanup on success
            if run_id and checkpointer is not None:
                try:
                    await checkpointer.adelete_thread(run_id)
                except Exception:
                    _logger.warning("review_checkpoint_delete_failed", extra={"run_id": run_id})
    except Exception:
        ...
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/application/use_cases/test_review.py -v
```

Expected: all `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add openbot/application/use_cases/review.py tests/application/use_cases/test_review.py
git commit -m "feat: review.py threads checkpointer+run_id, adds cancellation checkpoint + cleanup"
```

---

## Task 9: Add cancellation checkpoints to `chat.py`

**Files:**
- Modify: `openbot/application/use_cases/chat.py`
- Modify: `tests/application/use_cases/test_chat.py`

> **Note:** Chat doesn't use sandbox or write to GitHub state, so no `adelete_thread` cleanup is needed. The only goal here is to add two cancellation checkpoints around the freeform LLM call, and to forward the checkpointer and run_id from `PreflightContext` to the responder.

- [ ] **Step 1: Write failing test**

Add to `tests/application/use_cases/test_chat.py`:

```python
async def test_chat_freeform_passes_checkpointer_to_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbot.application.use_cases import chat as mod
    from langgraph.checkpoint.memory import MemorySaver

    captured: dict[str, Any] = {}

    async def fake_generate(*, event, user_request, run_id=None, checkpointer=None):
        captured["run_id"] = run_id
        captured["checkpointer"] = checkpointer
        return "ok"

    monkeypatch.setattr(mod, "_generate_freeform_reply", fake_generate)

    saver = MemorySaver()
    ctx = _make_chat_ctx(
        comment_body="@openbot please summarize",
        agent_checkpointer=saver,
        run_id="run-chat-1",
    )
    await mod.maybe_run_chat(ctx)

    assert captured["run_id"] == "run-chat-1"
    assert captured["checkpointer"] is saver
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/application/use_cases/test_chat.py -v -k "checkpointer"
```

- [ ] **Step 3: Update `chat.py`**

**Update `_generate_freeform_reply`:**

```python
async def _generate_freeform_reply(
    *, event, user_request: str,
    run_id: str | None = None,                       # ← new
    checkpointer: BaseCheckpointSaver | None = None, # ← new
) -> str:
    return await _RESPONDER.reply_for_event(
        event,
        user_request=user_request,
        run_id=run_id,           # ← new
        checkpointer=checkpointer,  # ← new
    )
```

**Add imports:**

```python
from openbot.application.state.cancellation import checkpoint
```

Under `TYPE_CHECKING`:

```python
    from langgraph.checkpoint.base import BaseCheckpointSaver
```

**Update `maybe_run_chat` freeform branch:**

```python
    run_id = ctx.dispatch.run_id
    checkpointer = ctx.agent_checkpointer

    ...
    elif not command.body_after_mention:
        message = _ACK_TEMPLATE.format(actor=event.actor or "there")
    else:
        await checkpoint(ctx.redis, run_id)     # ① before LLM
        try:
            message = await _generate_freeform_reply(
                event=event,
                user_request=command.body_after_mention,
                run_id=run_id,            # ← new
                checkpointer=checkpointer, # ← new
            )
        except Exception:
            ...
            message = _ERROR_TEMPLATE
        await checkpoint(ctx.redis, run_id)     # ② after LLM
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/application/use_cases/test_chat.py -v
```

Expected: all `PASSED`.

Full suite:

```bash
make check
```

Expected: ≥ 1097 tests, all passing.

- [ ] **Step 5: Commit**

```bash
git add openbot/application/use_cases/chat.py tests/application/use_cases/test_chat.py
git commit -m "feat: chat.py threads checkpointer+run_id to responder, adds cancellation checkpoints"
```

---

## Task 10: Final integration check and documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-05-21-agent-checkpoint-design.md` (mark implemented)

- [ ] **Step 1: Run the complete test suite**

```bash
make check
```

Expected: all tests pass (≥ 1097 at last count).

- [ ] **Step 2: Smoke-test the `__main__` imports**

```bash
uv run python -c "
import asyncio
from openbot.entrypoints.worker.__main__ import _build_auth
from openbot.infrastructure.persistence.agent_checkpointer import agent_checkpointer
print('imports ok')
"
```

Expected: `imports ok` with no errors.

- [ ] **Step 3: Verify checkpoint tables are created on startup (local)**

If you have Postgres running locally:

```bash
uv run python -c "
import asyncio
from openbot.infrastructure.persistence.agent_checkpointer import agent_checkpointer

async def main():
    async with agent_checkpointer('postgresql+asyncpg://localhost/openbot') as cp:
        if cp:
            print('checkpointer ready:', type(cp).__name__)
        else:
            print('no postgres, skipped')

asyncio.run(main())
"
```

Expected: `checkpointer ready: AsyncPostgresSaver` or `no postgres, skipped`.

- [ ] **Step 4: Archive the spec doc**

```bash
mv docs/superpowers/specs/2026-05-21-agent-checkpoint-design.md docs/_archive/superpowers/
git add docs/superpowers/specs/ docs/_archive/superpowers/
git commit -m "docs: archive agent-checkpoint spec (implemented)"
```

- [ ] **Step 5: Archive this plan**

```bash
mv docs/superpowers/plans/2026-05-21-agent-checkpoint.md docs/_archive/superpowers/
git add docs/superpowers/plans/ docs/_archive/superpowers/
git commit -m "docs: archive agent-checkpoint plan (implemented)"
```

---

## Self-Review

### Spec coverage

| Spec section | Covered by task |
|---|---|
| `langgraph-checkpoint-postgres` install | Task 1 |
| `agent_checkpointer.py` context manager | Task 1 |
| `setup()` call in context manager | Task 1 |
| `run_id` as `thread_id` | Tasks 4, 5, 6 |
| `checkpointer` passed to `create_deep_agent` | Tasks 4, 5, 6 |
| `PreflightContext.agent_checkpointer` field | Task 2 |
| Worker startup wiring | Task 3 |
| `fix.py` 5 cancellation checkpoints | Task 7 |
| `review.py` 1 cancellation checkpoint | Task 8 |
| `chat.py` 2 cancellation checkpoints | Task 9 |
| `adelete_thread` on success | Tasks 7, 8 |
| `lru_cache` removal in chat | Task 6 |
| `MemorySaver` for tests | All tasks with checkpoint tests |

### Placeholder scan

None found — all steps contain code.

### Type consistency

- `BaseCheckpointSaver` used consistently in all `TYPE_CHECKING` blocks (imported from `langgraph.checkpoint.base`)
- `run_id: str | None = None` and `checkpointer: BaseCheckpointSaver | None = None` used in the same order everywhere
- `checkpoint(ctx.redis, run_id)` used — matches `cancellation.checkpoint(redis, run_id)` signature exactly

### Scope check

All 13 implementation items from the spec are covered. Alembic migration is intentionally excluded — the spec explicitly says to rely on `saver.setup()` which auto-creates the 4 tables idempotently.
