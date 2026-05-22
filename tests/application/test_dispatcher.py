"""Dispatcher — classifier wiring + sandbox provisioning.

Covers Part 2 of plan ``2026-05-21-unified-sandbox-entry-plan.md``:

  - Task 2.1 (this file, this slice) — ``run_dispatch`` and
    ``execute_handler`` call the classifier once after preflight
    passes, populate ``ctx.classifier_output`` via
    ``dataclasses.replace``, and stay fail-open on classifier errors.

  - Task 2.2 (future) — provisioning + OR-merged policy gate.

The dispatcher's three sibling entry points share a single classifier
helper (``classify_for_dispatch``); these tests assert the helper's
return value reaches the handler context. Mocking is at the
``openbot.application.dispatcher`` import boundary so we don't go
through litellm — the classifier's own LLM contract is exercised in
``tests/application/dispatcher/test_classifier.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from openbot.application.dispatcher import execute_handler, run_dispatch
from openbot.application.middleware import (
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
)
from openbot.application.router import Dispatch
from openbot.dispatcher.classifier import (
    ChatClassifierOutput,
    TriageClassifierOutput,
)
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature


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
