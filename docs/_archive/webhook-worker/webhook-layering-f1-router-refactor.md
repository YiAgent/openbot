# Webhook-Worker Layering — F1: Chain Front-Migration (Part 2a/3: decide_and_enqueue + BackgroundTask)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Part 1:** `2026-05-20-webhook-worker-layering-f1-part1.md` — Tasks 1–3 (TaskSpec v3 schema, QueuePort, execute_handler)
>
> **Part 2b:** `2026-05-20-webhook-worker-layering-f1-part2b.md` — Tasks 6–8 (worker v3 path, F-series tests, final verification)

**Goal:** Implement `decide_and_enqueue()` (webhook async segment) and wire it into the BackgroundTask path, replacing the old `run_dispatch()` call at the webhook boundary.

**Spec:** `docs/specs/2026-05-17-webhook-worker-layering-design.md` §2 (webhook flow) and §8 F1 acceptance criteria.

---

## Task 4: decide_and_enqueue() — webhook async segment

**Files:**
- Create: `openbot/dispatcher/__init__.py`
- Create: `openbot/dispatcher/decide.py`
- Create: `tests/application/dispatcher/test_decide.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/application/dispatcher/test_decide.py
"""decide_and_enqueue — webhook async segment: preflight + TaskSpec v3 enqueue."""
from __future__ import annotations
import pytest
from dataclasses import replace
from openbot.dispatcher import decide_and_enqueue
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests._fakes.queue import FakeQueue
from tests.conftest import make_issue_opened_event


@pytest.mark.asyncio
async def test_decide_and_enqueue_builds_task_spec() -> None:
    """Happy path: no blocking middleware → TaskSpec v3 on FakeQueue."""
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

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.spec_version == 3
    assert spec.repo == event.repo
    assert spec.delivery_id == event.delivery_id
    assert spec.classifier_skipped is True
    assert spec.stages_to_run == []


@pytest.mark.asyncio
async def test_decide_and_enqueue_falls_back_in_process_when_no_queue() -> None:
    """No queue provided → handler runs in-process via execute_handler."""
    event = make_issue_opened_event()
    from openbot.application.router import dispatch_for
    dispatch = dispatch_for(event)
    assert dispatch is not None
    called: list[str] = []

    async def fake_handler(ctx) -> None:
        called.append(ctx.event.delivery_id)

    dispatch = replace(dispatch, handler=fake_handler)

    await decide_and_enqueue(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=None,
        session_factory=None,
        redis=None,
    )

    assert called == [event.delivery_id]


@pytest.mark.asyncio
async def test_decide_and_enqueue_never_raises() -> None:
    """Any internal exception must be swallowed — BackgroundTask contract."""
    event = make_issue_opened_event()
    from openbot.application.router import dispatch_for
    dispatch = dispatch_for(event)
    assert dispatch is not None

    # Corrupt queue to force an error path
    class ExplodingQueue:
        async def enqueue(self, *a, **kw) -> str: return "0-0"
        async def enqueue_task_spec(self, spec) -> str:
            raise RuntimeError("redis down")

    await decide_and_enqueue(
        adapter=FakeChannelAdapter(),
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=ExplodingQueue(),
        session_factory=None,
        redis=None,
    )


@pytest.mark.asyncio
async def test_decide_and_enqueue_initial_labels_extracted() -> None:
    """Labels from raw GitHub payload reach the TaskSpec."""
    from openbot.domain.events import EventKind, UnifiedEvent
    from openbot.application.router import dispatch_for
    raw_with_labels = {
        "issue": {
            "number": 1,
            "labels": [{"name": "cancel-openbot"}, {"name": "bug"}],
        }
    }
    event = UnifiedEvent(
        channel="github", delivery_id="del-labels", kind=EventKind.ISSUE_OPENED,
        repo="org/repo", actor="alice", actor_type=None,
        issue_number=1, pr_number=None, comment_body=None,
        installation_id=42, event_seq=0, raw=raw_with_labels,
    )
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

    assert len(queue.task_specs) == 1
    assert "cancel-openbot" in queue.task_specs[0].initial_labels
    assert "bug" in queue.task_specs[0].initial_labels
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**

```bash
cd /Users/wy/projects/openbot && uv run pytest tests/application/dispatcher/test_decide.py -v 2>&1 | head -10
```

Expected: `ModuleNotFoundError: No module named 'openbot.dispatcher'`

- [ ] **Step 3: Create the dispatcher package**

```python
# openbot/dispatcher/__init__.py
"""openbot.dispatcher — webhook async segment.

Runs D1-D9 preflight, builds TaskSpec v3, and enqueues to Redis Stream.
The worker receives the TaskSpec and calls execute_handler() directly.
"""
from openbot.dispatcher.decide import decide_and_enqueue

__all__ = ["decide_and_enqueue"]
```

- [ ] **Step 4: Implement decide.py**

```python
# openbot/dispatcher/decide.py
"""decide_and_enqueue — webhook async segment (design spec §2, D1-D9).

Runs the full preflight chain on the webhook async path, then builds a
TaskSpec v3 and enqueues it. The worker skips preflight and goes straight
to the handler. Falls back to in-process execution when no queue is
configured (dev / unit-test mode).

Never raises out — callers (BackgroundTask, tests) expect fire-and-forget.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openbot.application.dispatcher import build_preflight_chain, execute_handler
from openbot.application.middleware import MiddlewareResult, PreflightContext, run_preflight
from openbot.infrastructure.config_loader import load_for_repo
from openbot.infrastructure.queue.task_spec import TaskSpec

if TYPE_CHECKING:
    import redis.asyncio as redis_async
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from openbot.application.ports.audit_log import AuditLogPort
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.application.ports.config_loader import ConfigLoaderPort
    from openbot.application.ports.queue import QueuePort
    from openbot.application.ports.rate_limiter import RateLimiterPort
    from openbot.application.router import Dispatch
    from openbot.domain.events import UnifiedEvent

_logger = logging.getLogger(__name__)


def _extract_initial_labels(raw: dict) -> list[str]:
    """Best-effort label extraction from raw GitHub event payload.

    Checks issue.labels and pull_request.labels. Returns [] on any error.
    Used by the worker's W1 cancel quick-check.
    """
    for src in (raw.get("issue", {}), raw.get("pull_request", {})):
        if not isinstance(src, dict):
            continue
        labels = src.get("labels")
        if isinstance(labels, list):
            return [
                lbl.get("name", "")
                for lbl in labels
                if isinstance(lbl, dict) and lbl.get("name")
            ]
    return []


async def decide_and_enqueue(
    *,
    adapter: ChannelAdapterPort,
    event: UnifiedEvent,
    dispatch: Dispatch,
    config_loader: ConfigLoaderPort | None,
    queue: QueuePort | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    redis: redis_async.Redis | None,
    check_run_id: int | None = None,
    audit: AuditLogPort | None = None,
    rate_limiter: RateLimiterPort | None = None,
) -> None:
    """Webhook async segment: run D1-D9 preflight, build TaskSpec v3, enqueue.

    On PROCEED with a queue available: builds TaskSpec v3 and XADD's it.
    On PROCEED without a queue: calls execute_handler() in-process (dev fallback).
    On BLOCKED: returns silently (middleware already wrote the reply).

    Never raises out.
    """
    try:
        # D1: Load effective config for this repo.
        if config_loader is not None:
            config = await config_loader.load_for_repo(None, event)
        else:
            config = await load_for_repo(None, event)

        # D2-D9: Run the preflight chain (same chain the worker used to run).
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
        )
        chain = build_preflight_chain()
        result = await run_preflight(chain, ctx)

        if result is not MiddlewareResult.PROCEED:
            # Middleware handled the reply (block, rate-limit, etc.). Nothing to enqueue.
            return

        initial_labels = _extract_initial_labels(event.raw)

        if queue is not None:
            # Happy path: build TaskSpec v3 and push to Redis Stream.
            spec = TaskSpec.from_event_and_dispatch(
                event,
                dispatch,
                check_run_id=check_run_id,
                decision_trace=[],   # F1: trace populated in F3
                initial_labels=initial_labels,
            )
            await queue.enqueue_task_spec(spec)
            _logger.info(
                "decide_and_enqueue_queued",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    "scenario": spec.scenario,
                    "task_id": spec.task_id,
                },
            )
        else:
            # Dev fallback: no Redis queue; run handler in-process.
            _logger.debug(
                "decide_and_enqueue_in_process_fallback",
                extra={"delivery_id": event.delivery_id},
            )
            await execute_handler(
                adapter=adapter,
                event=event,
                dispatch=dispatch,
                config=config,
                session_factory=session_factory,
                redis=redis,
                check_run_id=check_run_id,
                audit=audit,
                rate_limiter=rate_limiter,
            )

    except Exception:
        _logger.exception(
            "decide_and_enqueue_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
```

- [ ] **Step 5: Run — expect 4 PASS**

```bash
cd /Users/wy/projects/openbot && uv run pytest tests/application/dispatcher/test_decide.py -v
```

- [ ] **Step 6: Run full suite**

```bash
cd /Users/wy/projects/openbot && make check 2>&1 | tail -5
```

Expected: ≥705 tests, all PASS.

- [ ] **Step 7: Commit**

```bash
cd /Users/wy/projects/openbot && git add openbot/dispatcher/__init__.py openbot/dispatcher/decide.py tests/application/dispatcher/test_decide.py
git commit -m "feat(dispatcher): add decide_and_enqueue() webhook async segment"
```

---

## Task 5: Wire BackgroundTask to decide_and_enqueue

**Files:**
- Modify: `openbot/entrypoints/api/routes/github_webhook.py`

The current `_run_dispatch` calls `run_dispatch()` which runs preflight + handler in the webhook async context. Replace it with `decide_and_enqueue()` which runs preflight + enqueues TaskSpec v3 (with in-process fallback).

- [ ] **Step 1: Read current imports in github_webhook.py**

Check what the top of `github_webhook.py` currently imports:

```bash
cd /Users/wy/projects/openbot && head -30 openbot/entrypoints/api/routes/github_webhook.py
```

- [ ] **Step 2: Replace the import and helper function**

Edit `openbot/entrypoints/api/routes/github_webhook.py`:

Replace the import line:
```python
# OLD
from openbot.application.dispatcher import run_dispatch
```

With:
```python
# NEW
from openbot.dispatcher import decide_and_enqueue
```

Replace the `_run_dispatch` helper:
```python
# OLD
async def _run_dispatch(
    app_instance: object,
    adapter: GitHubAdapter,
    event: UnifiedEvent,
    dispatch: Dispatch,
    check_run_id: int | None = None,
    audit: object | None = None,
) -> None:
    """In-process dispatch — used as the fallback when Redis is absent.

    Production runs through the Redis queue worker (``openbot.infrastructure.queue.runner``)
    instead; this path exists so `make dev` without docker-compose still
    delivers a working bot and so unit tests don't need fakeredis just
    to exercise the webhook flow.

    The actual middleware chain + handler invocation lives in
    ``openbot.application.dispatcher.run_dispatch`` so the worker and the webapp can't
    drift apart.
    """
    await run_dispatch(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        session_factory=getattr(app_instance.state, "db_session_factory", None),
        redis=getattr(app_instance.state, "redis", None),
        check_run_id=check_run_id,
        audit=audit,
        rate_limiter=getattr(app_instance.state, "rate_limiter", None),
        config_loader=getattr(app_instance.state, "config_loader", None),
    )
```

With:
```python
# NEW
async def _decide_and_enqueue_bg(
    app_instance: object,
    adapter: GitHubAdapter,
    event: UnifiedEvent,
    dispatch: Dispatch,
    check_run_id: int | None = None,
    audit: object | None = None,
) -> None:
    """Webhook async segment — runs preflight then enqueues TaskSpec v3.

    Falls back to in-process execution when Redis is absent (dev / CI).
    Production: preflight runs here, handler runs in the queue worker.
    """
    state = app_instance.state
    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=getattr(state, "config_loader", None),
        queue=getattr(state, "queue", None),
        session_factory=getattr(state, "db_session_factory", None),
        redis=getattr(state, "redis", None),
        check_run_id=check_run_id,
        audit=audit,
        rate_limiter=getattr(state, "rate_limiter", None),
    )
```

Also update the `background.add_task` call site — change `_run_dispatch` to `_decide_and_enqueue_bg`:

```python
# OLD
background.add_task(
    _run_dispatch,
    request.app,
    adapter,
    bd.event,
    bd.dispatch,
    bd.check_run_id,
    getattr(state, "audit", None),
)
```

```python
# NEW
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

- [ ] **Step 3: Run webhook route tests**

```bash
cd /Users/wy/projects/openbot && uv run pytest tests/entrypoints/ -v -k "webhook" 2>&1 | tail -20
```

Expected: All PASS. The webhook tests use FakeQueue so `decide_and_enqueue()` will hit the in-process fallback path (no queue in test state) — same behaviour as before.

- [ ] **Step 4: Run full suite**

```bash
cd /Users/wy/projects/openbot && make check 2>&1 | tail -5
```

Expected: ≥705 tests, all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/wy/projects/openbot && git add openbot/entrypoints/api/routes/github_webhook.py
git commit -m "feat(webhook): wire BackgroundTask to decide_and_enqueue() (F1)"
```

---

> **Continue to Part 2b:** `2026-05-20-webhook-worker-layering-f1-part2b.md`
> Tasks 6–8: worker v3 path, F-series tests, final verification.
