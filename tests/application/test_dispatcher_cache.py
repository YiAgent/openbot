"""Dispatcher — SandboxCachePort integration matrix (Task 2.3).

Covers every branch in the cache-aware section of ``_run_with_sandbox``:

  +---------------------------+-----------------------------+--------------------------------+
  | Scenario                  | acquire returns             | Expected                       |
  +---------------------------+-----------------------------+--------------------------------+
  | no cache wired            | (not called)                | factory runs; no cache metrics |
  | miss                      | None                        | factory runs; publish fires    |
  | hit                       | SandboxedHandle             | handler gets hit handle        |
  | backend_error             | raises Exception            | backend_error counter; factory |
  | handler raises on miss    | None                        | publish still fires (finally)  |
  +---------------------------+-----------------------------+--------------------------------+

Metrics are asserted via ``MagicMock`` substitutions at the dispatcher
module boundary (same pattern as ``test_dispatcher_observability.py``).

``asyncio.create_task`` for ``_safe_publish`` is fire-and-forget; tests
drain the event loop with ``await asyncio.sleep(0)`` after
``run_dispatch`` completes before asserting publish call counts.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openbot.application.dispatcher import execute_handler
from openbot.application.middleware import PreflightContext
from openbot.application.router import Dispatch
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.domain.checkout import CheckoutSpec, CloneStrategy
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from tests._fakes.sandbox import FakeSandboxLifecycle

# ── Fixtures / helpers ────────────────────────────────────────────────────────


def _event(**extra: Any) -> UnifiedEvent:
    base: dict[str, Any] = {
        "channel": "github",
        "delivery_id": "d-cache",
        "kind": EventKind.ISSUE_OPENED,
        "repo": "acme/widget",
        "actor": "alice",
        "issue_number": 42,
        "installation_id": 99,
    }
    base.update(extra)
    return UnifiedEvent(**base)


def _mock_checkout(monkeypatch: pytest.MonkeyPatch) -> CheckoutSpec:
    spec = CheckoutSpec(
        repo_url="https://github.com/acme/widget.git",
        ref="deadbeef",
        strategy=CloneStrategy.SHALLOW,
    )
    monkeypatch.setattr(
        "openbot.application.dispatcher.resolve_checkout",
        AsyncMock(return_value=spec),
    )
    return spec


def _record_counter(monkeypatch: pytest.MonkeyPatch, attr: str) -> MagicMock:
    """Replace ``openbot.application.dispatcher.<attr>`` with a recording MagicMock."""
    counter = MagicMock()
    monkeypatch.setattr(f"openbot.application.dispatcher.{attr}", counter)
    return counter


def _factory_from_sandbox(sandbox: FakeSandboxLifecycle) -> Any:
    @asynccontextmanager
    async def _cm():
        try:
            yield sandbox
        finally:
            await sandbox.close()

    return _cm


def _make_cache(
    *, acquire_result: SandboxedHandle | None = None, acquire_raises: Exception | None = None
) -> AsyncMock:
    """Build an AsyncMock SandboxCachePort with scripted acquire behaviour."""
    cache = AsyncMock()
    if acquire_raises is not None:
        cache.acquire = AsyncMock(side_effect=acquire_raises)
    else:
        cache.acquire = AsyncMock(return_value=acquire_result)
    cache.publish = AsyncMock(return_value=None)
    return cache


def _fix_dispatch(handler: Any) -> Dispatch:
    return Dispatch(feature=Feature.FIX, task_id="t-cache-1", handler=handler)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_cache_wired_runs_cold_path_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``ctx.sandbox_cache is None`` the cache branch is skipped entirely.

    No cache metrics should fire; the factory is called and the handler
    receives a populated ``SandboxedHandle`` as before.
    """
    _mock_checkout(monkeypatch)
    cache_total = _record_counter(monkeypatch, "sandbox_cache_total")
    cache_publish_total = _record_counter(monkeypatch, "sandbox_cache_publish_total")

    sandbox = FakeSandboxLifecycle()
    adapter = AsyncMock()
    adapter.get_installation_token = AsyncMock(return_value="ghs_t")

    seen: dict[str, Any] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen["handle"] = ctx.sandbox_handle

    await execute_handler(
        adapter=adapter,
        event=_event(),
        dispatch=_fix_dispatch(handler),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        sandbox_factory=_factory_from_sandbox(sandbox),
        # sandbox_cache not passed → defaults to None inside PreflightContext
    )

    # Handler received a real handle from the factory path.
    assert seen["handle"] is not None

    # No cache metrics emitted.
    cache_total.labels.assert_not_called()
    cache_publish_total.labels.assert_not_called()


@pytest.mark.asyncio
async def test_cache_miss_falls_through_to_factory_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``acquire`` returns ``None`` (miss) → cold path runs → ``publish``
    fires asynchronously via ``create_task``.

    Asserts:
      - ``cache_total{feature=fix, result=miss}`` incremented.
      - Handler receives a handle from the factory (not a cache handle).
      - ``cache.publish`` is awaited exactly once with the correct installation_id.
      - ``cache_publish_total{feature=fix, result=created}`` incremented.
    """
    _mock_checkout(monkeypatch)
    cache_total = _record_counter(monkeypatch, "sandbox_cache_total")
    cache_publish_total = _record_counter(monkeypatch, "sandbox_cache_publish_total")
    _record_counter(monkeypatch, "sandbox_cache_acquire_seconds")

    cache = _make_cache(acquire_result=None)
    sandbox = FakeSandboxLifecycle()
    adapter = AsyncMock()
    adapter.get_installation_token = AsyncMock(return_value="ghs_t")

    seen: dict[str, Any] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen["handle"] = ctx.sandbox_handle

    await execute_handler(
        adapter=adapter,
        event=_event(),
        dispatch=_fix_dispatch(handler),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        sandbox_factory=_factory_from_sandbox(sandbox),
        sandbox_cache=cache,
    )
    # Drain create_task queue so _safe_publish completes.
    await asyncio.sleep(0)

    # Cold path: handler got a real factory-produced handle.
    assert seen["handle"] is not None

    # Miss counter.
    cache_total.labels.assert_any_call(feature="fix", result="miss")

    # Publish was called once with installation_id from the event.
    cache.publish.assert_awaited_once()
    publish_call = cache.publish.await_args
    assert publish_call.kwargs["installation_id"] == 99

    # Publish success counter.
    cache_publish_total.labels.assert_any_call(feature="fix", result="created")


@pytest.mark.asyncio
async def test_cache_hit_bypasses_factory_and_calls_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``acquire`` returns a ``SandboxedHandle`` (hit) → handler is called
    with the hit handle and the factory is never opened.

    Asserts:
      - ``cache_total{feature=fix, result=hit}`` incremented.
      - ``sandbox_cache_acquire_seconds{result=hit}`` observed.
      - ``cache.publish`` is NOT called (we own the handle, no publish needed).
      - Handler's ``ctx.sandbox_handle`` is exactly the object ``acquire`` returned.
    """
    _mock_checkout(monkeypatch)
    cache_total = _record_counter(monkeypatch, "sandbox_cache_total")
    cache_acquire_seconds = _record_counter(monkeypatch, "sandbox_cache_acquire_seconds")
    cache_publish_total = _record_counter(monkeypatch, "sandbox_cache_publish_total")

    hit_handle = SandboxedHandle(
        sandbox=AsyncMock(),
        checkout=CheckoutSpec(
            repo_url="https://github.com/acme/widget.git",
            ref="deadbeef",
            strategy=CloneStrategy.SHALLOW,
        ),
        token="ghs_cached",
    )
    cache = _make_cache(acquire_result=hit_handle)

    # Factory must NOT be called on a hit.
    factory_called = False

    @asynccontextmanager
    async def _factory_that_must_not_run():
        nonlocal factory_called
        factory_called = True
        yield AsyncMock()  # pragma: no cover

    adapter = AsyncMock()
    adapter.get_installation_token = AsyncMock(return_value="ghs_t")

    seen: dict[str, Any] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen["handle"] = ctx.sandbox_handle

    await execute_handler(
        adapter=adapter,
        event=_event(),
        dispatch=_fix_dispatch(handler),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        sandbox_factory=_factory_that_must_not_run,
        sandbox_cache=cache,
    )
    await asyncio.sleep(0)

    assert not factory_called, "Factory must not open on a cache hit"
    assert seen["handle"] is hit_handle

    cache_total.labels.assert_any_call(feature="fix", result="hit")
    cache_acquire_seconds.labels.assert_any_call(result="hit")
    cache_acquire_seconds.labels.return_value.observe.assert_called_once()

    # No publish on a hit — we didn't clone.
    cache_publish_total.labels.assert_not_called()
    cache.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_backend_error_falls_through_to_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``acquire`` raises an unexpected exception → ``backend_error`` counter
    fires, the dispatcher falls through to the cold path, and the handler
    still receives a valid handle.

    The exception must not propagate out of ``run_dispatch``.
    """
    _mock_checkout(monkeypatch)
    cache_total = _record_counter(monkeypatch, "sandbox_cache_total")
    _record_counter(monkeypatch, "sandbox_cache_publish_total")
    _record_counter(monkeypatch, "sandbox_cache_acquire_seconds")

    cache = _make_cache(acquire_raises=RuntimeError("daytona exploded"))
    sandbox = FakeSandboxLifecycle()
    adapter = AsyncMock()
    adapter.get_installation_token = AsyncMock(return_value="ghs_t")

    seen: dict[str, Any] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen["handle"] = ctx.sandbox_handle

    await execute_handler(
        adapter=adapter,
        event=_event(),
        dispatch=_fix_dispatch(handler),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        sandbox_factory=_factory_from_sandbox(sandbox),
        sandbox_cache=cache,
    )
    await asyncio.sleep(0)

    # Cold path still ran.
    assert seen["handle"] is not None

    # Backend-error counter fired.
    cache_total.labels.assert_any_call(feature="fix", result="backend_error")


@pytest.mark.asyncio
async def test_publish_fires_in_finally_even_when_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a cache miss occurs and the handler raises, the dispatcher must
    still schedule ``_safe_publish`` so the warm pool is populated for
    the *next* request (handler exceptions are the handler's concern;
    the cache shouldn't miss a publish opportunity because of them).

    ``run_dispatch`` itself must not re-raise the handler exception.
    """
    _mock_checkout(monkeypatch)
    _record_counter(monkeypatch, "sandbox_cache_total")
    _record_counter(monkeypatch, "sandbox_cache_publish_total")
    _record_counter(monkeypatch, "sandbox_cache_acquire_seconds")

    cache = _make_cache(acquire_result=None)
    sandbox = FakeSandboxLifecycle()
    adapter = AsyncMock()
    adapter.get_installation_token = AsyncMock(return_value="ghs_t")

    async def exploding_handler(_ctx: PreflightContext) -> None:
        raise RuntimeError("handler bug")

    await execute_handler(
        adapter=adapter,
        event=_event(),
        dispatch=_fix_dispatch(exploding_handler),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        sandbox_factory=_factory_from_sandbox(sandbox),
        sandbox_cache=cache,
    )
    # Drain create_task queue.
    await asyncio.sleep(0)

    # run_dispatch must not raise (it's a "never raise out" contract).
    # Publish must have been attempted despite the handler crash.
    cache.publish.assert_awaited_once()
