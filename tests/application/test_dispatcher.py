"""Dispatcher — classifier wiring + sandbox provisioning.

Covers Part 2 of plan ``2026-05-21-unified-sandbox-entry-plan.md``:

  - Task 2.1 — ``run_dispatch`` and ``execute_handler`` call the
    classifier once after preflight passes, populate
    ``ctx.classifier_output`` via ``dataclasses.replace``, and stay
    fail-open on classifier errors.

  - Task 2.2 (this slice, added below) — OR-merge of static
    ``Dispatch.sandbox_policy`` with the classifier output via
    ``derive_sandbox_policy`` → provisioning block (resolve_checkout +
    adapter.get_installation_token + sandbox_factory + clone) →
    handler runs with a live ``SandboxedHandle`` or with
    ``ctx.sandbox_handle is None`` on every degrade path. Six error
    modes are covered: NO_SANDBOX (static), NO_SANDBOX (classifier
    OR-merge), ``sandbox_factory is None``, resolver raises,
    ``get_installation_token`` raises, ``sandbox.clone`` raises.

The dispatcher's three sibling entry points share a single classifier
helper (``classify_for_dispatch``); these tests assert the helper's
return value reaches the handler context. Mocking is at the
``openbot.application.dispatcher`` import boundary so we don't go
through litellm — the classifier's own LLM contract is exercised in
``tests/application/dispatcher/test_classifier.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openbot.application.dispatcher import execute_handler, run_dispatch
from openbot.application.middleware import (
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
)
from openbot.application.router import Dispatch, SandboxPolicy
from openbot.dispatcher.classifier import (
    ChatClassifierOutput,
    TriageClassifierOutput,
)
from openbot.domain.checkout import CheckoutResolutionError, CheckoutSpec, CloneStrategy
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from tests._fakes.sandbox import FakeSandboxLifecycle


def _event(*, kind: EventKind = EventKind.ISSUE_OPENED, **extra: Any) -> UnifiedEvent:
    base: dict[str, Any] = {
        "channel": "github",
        "delivery_id": "d-1",
        "kind": kind,
        "repo": "acme/widget",
        "actor": "alice",
        "issue_number": 42,
    }
    base.update(extra)
    return UnifiedEvent(**base)


def _proceed_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short-circuit config load + preflight to PROCEED.

    Both helpers are looked up via ``getattr`` on the dispatcher module
    inside ``run_dispatch`` (so monkeypatching the module attribute
    wins over the import-time binding); patching the module attribute
    directly is the supported customisation point.
    """
    monkeypatch.setattr(
        "openbot.application.dispatcher.load_for_repo",
        AsyncMock(return_value=AsyncMock()),
    )
    monkeypatch.setattr(
        "openbot.application.dispatcher.run_preflight",
        AsyncMock(return_value=MiddlewareDecision.proceed()),
    )


@pytest.mark.asyncio
async def test_run_dispatch_calls_classifier_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight passes → classifier is invoked exactly once with the
    feature + redis args from the dispatch call. The result then lands
    on ``ctx.classifier_output`` (asserted in the next test)."""
    _proceed_preflight(monkeypatch)

    classify_mock = AsyncMock(
        return_value=TriageClassifierOutput(
            type="bug",
            severity_guess="medium",
            has_reproduction_info=True,
            looks_like_spam=False,
        )
    )
    monkeypatch.setattr("openbot.application.dispatcher.classify_for_dispatch", classify_mock)

    async def handler(ctx: PreflightContext) -> None:
        pass

    await run_dispatch(
        adapter=AsyncMock(),
        event=_event(),
        dispatch=Dispatch(feature=Feature.TRIAGE, task_id="t-1", handler=handler),
        session_factory=None,
        redis=None,
    )

    assert classify_mock.await_count == 1
    assert classify_mock.await_args is not None
    kwargs = classify_mock.await_args.kwargs
    assert kwargs["feature"] is Feature.TRIAGE
    assert kwargs["redis"] is None


@pytest.mark.asyncio
async def test_run_dispatch_threads_classifier_output_into_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handler receives a ``PreflightContext`` whose
    ``classifier_output`` is the classifier's return value (not the
    raw dict — the typed dataclass)."""
    _proceed_preflight(monkeypatch)
    expected = TriageClassifierOutput(
        type="bug",
        severity_guess="high",
        has_reproduction_info=True,
        looks_like_spam=False,
    )
    monkeypatch.setattr(
        "openbot.application.dispatcher.classify_for_dispatch",
        AsyncMock(return_value=expected),
    )

    seen_ctx: dict[str, PreflightContext] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen_ctx["ctx"] = ctx

    await run_dispatch(
        adapter=AsyncMock(),
        event=_event(),
        dispatch=Dispatch(feature=Feature.TRIAGE, task_id="t-1", handler=handler),
        session_factory=None,
        redis=None,
    )

    assert seen_ctx["ctx"].classifier_output is expected


@pytest.mark.asyncio
async def test_run_dispatch_classifier_output_none_when_helper_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`classify_for_dispatch` is fail-open — returns None on
    classifier error or for Feature.FIX. The dispatcher must propagate
    that None to the handler context (not crash, not raise)."""
    _proceed_preflight(monkeypatch)
    monkeypatch.setattr(
        "openbot.application.dispatcher.classify_for_dispatch",
        AsyncMock(return_value=None),
    )

    seen_ctx: dict[str, PreflightContext] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen_ctx["ctx"] = ctx

    await run_dispatch(
        adapter=AsyncMock(),
        event=_event(),
        dispatch=Dispatch(feature=Feature.FIX, task_id="t-1", handler=handler),
        session_factory=None,
        redis=None,
    )

    assert seen_ctx["ctx"].classifier_output is None


@pytest.mark.asyncio
async def test_run_dispatch_does_not_call_classifier_when_preflight_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight blocks → handler not called → classifier not called
    either (no point spending an LLM call when the workflow won't
    run)."""
    monkeypatch.setattr(
        "openbot.application.dispatcher.load_for_repo",
        AsyncMock(return_value=AsyncMock()),
    )
    monkeypatch.setattr(
        "openbot.application.dispatcher.run_preflight",
        AsyncMock(
            return_value=MiddlewareDecision(result=MiddlewareResult.BLOCKED, reason="cancel")
        ),
    )
    classify_mock = AsyncMock()
    monkeypatch.setattr("openbot.application.dispatcher.classify_for_dispatch", classify_mock)

    handler_calls = 0

    async def handler(ctx: PreflightContext) -> None:
        nonlocal handler_calls
        handler_calls += 1

    await run_dispatch(
        adapter=AsyncMock(),
        event=_event(),
        dispatch=Dispatch(feature=Feature.TRIAGE, task_id="t-1", handler=handler),
        session_factory=None,
        redis=None,
    )

    assert handler_calls == 0
    classify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_execute_handler_accepts_classifier_output_kwarg() -> None:
    """The worker path passes the already-deserialized classifier
    output from the TaskSpec — ``execute_handler`` threads it into ctx
    without re-classifying."""
    seen_ctx: dict[str, PreflightContext] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen_ctx["ctx"] = ctx

    expected = ChatClassifierOutput(
        intent="readonly_qa",
        needs_clarification=False,
        scope_hint=None,
    )

    await execute_handler(
        adapter=AsyncMock(),
        event=_event(),
        dispatch=Dispatch(feature=Feature.CHAT, task_id="t-2", handler=handler),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        classifier_output=expected,
    )

    assert seen_ctx["ctx"].classifier_output is expected


@pytest.mark.asyncio
async def test_execute_handler_defaults_classifier_output_to_none() -> None:
    """Backward compatibility: callers that haven't yet adopted the
    new kwarg still work — ``classifier_output`` defaults to None and
    the handler sees None on ctx."""
    seen_ctx: dict[str, PreflightContext] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen_ctx["ctx"] = ctx

    await execute_handler(
        adapter=AsyncMock(),
        event=_event(),
        dispatch=Dispatch(feature=Feature.TRIAGE, task_id="t-3", handler=handler),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
    )

    assert seen_ctx["ctx"].classifier_output is None


# ---------------------------------------------------------------------------
# Task 2.2 — sandbox provisioning + OR-merge policy gate
# ---------------------------------------------------------------------------
#
# These tests pin down the contract between ``derive_sandbox_policy`` /
# ``resolve_checkout`` / ``sandbox_factory`` / ``SandboxedHandle`` inside
# ``run_dispatch``. The same provisioning block also lives in
# ``execute_handler`` (worker path); a single focused test covers that
# symmetry at the end so we don't redo all five degrade matrices twice.
#
# Test rig: every test patches ``classify_for_dispatch`` and
# ``resolve_checkout`` at the dispatcher module boundary, builds a
# ``FakeSandboxLifecycle``-backed factory, and inspects the
# ``PreflightContext`` the handler observes. The dispatcher's "never
# raise out" contract means we never expect a degrade path to surface
# the exception — we assert the handler still runs and
# ``ctx.sandbox_handle is None``.


def _resolve_checkout_returning(spec: CheckoutSpec) -> AsyncMock:
    """An ``AsyncMock`` that yields ``spec``; usable as a ``resolve_checkout``
    monkeypatch target."""
    return AsyncMock(return_value=spec)


def _factory_from_sandbox(sandbox: FakeSandboxLifecycle) -> Any:
    """Build the sandbox_factory shape ``run_dispatch`` expects.

    ``ctx.sandbox_factory`` is a *callable* that returns an *async context
    manager* yielding a ``SandboxPort``. ``asynccontextmanager`` wraps an
    async generator into exactly that protocol; the inner ``async with``
    re-enters cleanly for the dispatcher.
    """

    @asynccontextmanager
    async def _cm():
        try:
            yield sandbox
        finally:
            await sandbox.close()

    return _cm


@pytest.mark.asyncio
async def test_dispatcher_provisions_sandbox_on_required_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path. Static REQUIRED + classifier returning a `bug` triage
    output (not a bypass case) → handler sees a populated
    ``SandboxedHandle``: same checkout the resolver returned, same token
    the adapter handed back, same open sandbox the factory yielded."""
    _proceed_preflight(monkeypatch)
    monkeypatch.setattr(
        "openbot.application.dispatcher.classify_for_dispatch",
        AsyncMock(
            return_value=TriageClassifierOutput(
                type="bug",
                severity_guess="medium",
                has_reproduction_info=True,
                looks_like_spam=False,
            )
        ),
    )
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

    await run_dispatch(
        adapter=adapter,
        event=_event(),
        dispatch=Dispatch(feature=Feature.TRIAGE, task_id="t-1", handler=handler),
        session_factory=None,
        redis=None,
        sandbox_factory=_factory_from_sandbox(sandbox),
    )

    handle = seen_ctx["ctx"].sandbox_handle
    assert handle is not None
    assert handle.checkout is spec
    assert handle.token == "ghs_token_42"
    assert handle.sandbox is sandbox
    assert sandbox.cloned == [("https://github.com/acme/widget.git", "deadbeef", "ghs_token_42")]
    assert sandbox.clone_strategies == [CloneStrategy.SHALLOW]
    assert sandbox.closed is True


@pytest.mark.asyncio
async def test_dispatcher_skips_provisioning_on_static_no_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route that statically declares ``NO_SANDBOX`` (label flips) must
    bypass the entire provisioning block — no resolver call, no token
    fetch, no factory call — even when the classifier would otherwise
    have voted REQUIRED."""
    _proceed_preflight(monkeypatch)
    monkeypatch.setattr(
        "openbot.application.dispatcher.classify_for_dispatch",
        AsyncMock(return_value=None),
    )
    resolver_mock = AsyncMock()
    monkeypatch.setattr("openbot.application.dispatcher.resolve_checkout", resolver_mock)

    sandbox = FakeSandboxLifecycle()
    factory_mock = AsyncMock(side_effect=AssertionError("factory must not be called"))

    seen_ctx: dict[str, PreflightContext] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen_ctx["ctx"] = ctx

    await run_dispatch(
        adapter=AsyncMock(),
        event=_event(kind=EventKind.ISSUE_LABELED),
        dispatch=Dispatch(
            feature=Feature.TRIAGE,
            task_id="t-1",
            handler=handler,
            sandbox_policy=SandboxPolicy.NO_SANDBOX,
        ),
        session_factory=None,
        redis=None,
        sandbox_factory=factory_mock,
    )

    assert seen_ctx["ctx"].sandbox_handle is None
    resolver_mock.assert_not_called()
    factory_mock.assert_not_called()
    assert sandbox.cloned == []


@pytest.mark.asyncio
async def test_dispatcher_skips_provisioning_on_classifier_unclear_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static REQUIRED + chat classifier ``intent='unclear'`` → OR-merge
    yields NO_SANDBOX → handler runs with ``sandbox_handle is None`` and
    ``classifier_output.intent == 'unclear'`` (so the handler can post a
    clarification reply without burning a clone)."""
    _proceed_preflight(monkeypatch)
    classifier_out = ChatClassifierOutput(
        intent="unclear", needs_clarification=True, scope_hint=None
    )
    monkeypatch.setattr(
        "openbot.application.dispatcher.classify_for_dispatch",
        AsyncMock(return_value=classifier_out),
    )
    resolver_mock = AsyncMock()
    monkeypatch.setattr("openbot.application.dispatcher.resolve_checkout", resolver_mock)

    sandbox = FakeSandboxLifecycle()

    seen_ctx: dict[str, PreflightContext] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen_ctx["ctx"] = ctx

    await run_dispatch(
        adapter=AsyncMock(),
        event=_event(kind=EventKind.ISSUE_COMMENT_CREATED),
        dispatch=Dispatch(feature=Feature.CHAT, task_id="t-1", handler=handler),
        session_factory=None,
        redis=None,
        sandbox_factory=_factory_from_sandbox(sandbox),
    )

    assert seen_ctx["ctx"].sandbox_handle is None
    assert seen_ctx["ctx"].classifier_output is classifier_out
    resolver_mock.assert_not_called()
    assert sandbox.cloned == []


@pytest.mark.asyncio
async def test_dispatcher_degrades_gracefully_on_clone_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sandbox.clone`` raises (bad token, network blip, missing ref) →
    we exit the ``async with`` cleanly and call the handler with
    ``sandbox_handle is None``. The exception MUST NOT propagate out of
    the dispatcher."""
    _proceed_preflight(monkeypatch)
    monkeypatch.setattr(
        "openbot.application.dispatcher.classify_for_dispatch",
        AsyncMock(return_value=None),
    )
    spec = CheckoutSpec(repo_url="https://x/y.git", ref="sha", strategy=CloneStrategy.SHALLOW)
    monkeypatch.setattr(
        "openbot.application.dispatcher.resolve_checkout", _resolve_checkout_returning(spec)
    )

    class _BrokenSandbox(FakeSandboxLifecycle):
        async def clone(self, **_: Any) -> None:
            raise RuntimeError("network blip")

    sandbox = _BrokenSandbox()
    adapter = AsyncMock()
    adapter.get_installation_token = AsyncMock(return_value="ghs_x")

    seen_ctx: dict[str, PreflightContext] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen_ctx["ctx"] = ctx

    await run_dispatch(
        adapter=adapter,
        event=_event(),
        dispatch=Dispatch(feature=Feature.TRIAGE, task_id="t-1", handler=handler),
        session_factory=None,
        redis=None,
        sandbox_factory=_factory_from_sandbox(sandbox),
    )

    assert seen_ctx["ctx"].sandbox_handle is None
    assert sandbox.closed is True  # context manager exited cleanly


@pytest.mark.asyncio
async def test_dispatcher_degrades_gracefully_on_factory_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ctx.sandbox_factory is None`` (sandbox backend not configured on
    this deployment) → handler still runs, with ``sandbox_handle is
    None``. No resolver call either — the absence of a factory makes
    the resolution pointless."""
    _proceed_preflight(monkeypatch)
    monkeypatch.setattr(
        "openbot.application.dispatcher.classify_for_dispatch",
        AsyncMock(return_value=None),
    )
    resolver_mock = AsyncMock()
    monkeypatch.setattr("openbot.application.dispatcher.resolve_checkout", resolver_mock)

    seen_ctx: dict[str, PreflightContext] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen_ctx["ctx"] = ctx

    await run_dispatch(
        adapter=AsyncMock(),
        event=_event(),
        dispatch=Dispatch(feature=Feature.TRIAGE, task_id="t-1", handler=handler),
        session_factory=None,
        redis=None,
        sandbox_factory=None,
    )

    assert seen_ctx["ctx"].sandbox_handle is None
    resolver_mock.assert_not_called()


@pytest.mark.asyncio
async def test_dispatcher_degrades_gracefully_on_resolver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``resolve_checkout`` raises ``CheckoutResolutionError`` (unmatched
    event/workflow pair, missing clone_url) → handler runs with
    ``sandbox_handle is None``. We must NOT call the factory after a
    resolution failure — a no-checkout sandbox is worse than no sandbox
    at all."""
    _proceed_preflight(monkeypatch)
    monkeypatch.setattr(
        "openbot.application.dispatcher.classify_for_dispatch",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "openbot.application.dispatcher.resolve_checkout",
        AsyncMock(side_effect=CheckoutResolutionError("missing clone_url")),
    )

    sandbox = FakeSandboxLifecycle()
    factory_mock = AsyncMock(side_effect=AssertionError("factory must not be called"))

    seen_ctx: dict[str, PreflightContext] = {}

    async def handler(ctx: PreflightContext) -> None:
        seen_ctx["ctx"] = ctx

    await run_dispatch(
        adapter=AsyncMock(),
        event=_event(),
        dispatch=Dispatch(feature=Feature.TRIAGE, task_id="t-1", handler=handler),
        session_factory=None,
        redis=None,
        sandbox_factory=factory_mock,
    )

    assert seen_ctx["ctx"].sandbox_handle is None
    factory_mock.assert_not_called()
    assert sandbox.cloned == []


@pytest.mark.asyncio
async def test_execute_handler_provisions_sandbox_symmetrically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker entry point goes through the same provisioning block.
    ``execute_handler`` doesn't run the classifier itself (it was
    rehydrated from the TaskSpec), so we feed the classifier output in
    via the kwarg and assert the resulting ``SandboxedHandle`` shape.
    Single test for symmetry — the degrade matrix is identical, and is
    already exercised on ``run_dispatch`` above."""
    spec = CheckoutSpec(repo_url="https://x/y.git", ref="cafe", strategy=CloneStrategy.SHALLOW)
    monkeypatch.setattr(
        "openbot.application.dispatcher.resolve_checkout", _resolve_checkout_returning(spec)
    )

    sandbox = FakeSandboxLifecycle()
    adapter = AsyncMock()
    adapter.get_installation_token = AsyncMock(return_value="ghs_w")

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
    assert handle.token == "ghs_w"
    assert sandbox.cloned == [("https://x/y.git", "cafe", "ghs_w")]
