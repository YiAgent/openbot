"""Dispatcher -- Prometheus + structured-log signals for the sandbox-entry slice.

Closes Task 2.3 of ``2026-05-21-unified-sandbox-entry-plan.md``. The
provisioning branches landed in Task 2.2 -- these tests pin down the
*observability* contract:

  - ``openbot_dispatch_sandbox_total{feature, policy, bypass_source}``
    fires exactly once per dispatch with the correct labels. Four
    distinct ``bypass_source`` values, one per code path:

      * ``none``       -- happy path; sandbox provisioned + clone OK.
      * ``static``     -- ``Dispatch.sandbox_policy = NO_SANDBOX``.
      * ``classifier`` -- OR-merge with the classifier said skip.
      * ``degrade``    -- provisioning attempted but failed (resolver,
                         token fetch, factory, or clone).

  - ``openbot_classifier_error_total{feature}`` fires on the fail-open
    branch in ``classify_for_dispatch`` (any exception -> log + None +
    increment). The counter is the only signal ops have that the
    classifier is degrading -- the dispatcher itself never raises out.

The counters live on ``openbot.core.metrics`` (next to the existing
``workflow_total`` / ``llm_cost_usd_total``); tests monkeypatch the
*re-exported* names on the consumer modules so we don't depend on the
production prometheus registry state.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openbot.application.dispatcher import execute_handler
from openbot.application.middleware import PreflightContext
from openbot.application.router import Dispatch, SandboxPolicy
from openbot.dispatcher.classifier import ChatClassifierOutput, classify_for_dispatch
from openbot.domain.checkout import CheckoutResolutionError, CheckoutSpec, CloneStrategy
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from tests._fakes.sandbox import FakeSandboxLifecycle

# -- Test fixtures ─────────────────────────────────────────────────────


def _event(*, kind: EventKind = EventKind.ISSUE_OPENED, **extra: Any) -> UnifiedEvent:
    """Same shape as ``tests/application/test_dispatcher.py::_event``.

    Duplicated rather than imported because the two files cover
    different concerns; keeping the helper local also keeps the
    observability tests independent of whatever changes happen to the
    happy-path tests next door.
    """
    base: dict[str, Any] = {
        "channel": "github",
        "delivery_id": "d-obs",
        "kind": kind,
        "repo": "acme/widget",
        "actor": "alice",
        "issue_number": 42,
    }
    base.update(extra)
    return UnifiedEvent(**base)


def _record_counter(monkeypatch: pytest.MonkeyPatch, attr: str) -> MagicMock:
    """Replace ``openbot.application.dispatcher.<attr>`` with a recording
    MagicMock. Returns the mock so the test can introspect the
    ``.labels(...).inc()`` call chain.

    Counter implementations follow the prometheus ``labels(**kw).inc()``
    fluent shape, so we don't need a Protocol-shaped fake -- MagicMock
    happily returns itself from chained calls. The behaviour we care
    about is *which labels were passed*, and that's recorded on
    ``mock.labels.call_args_list``.
    """
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


# -- dispatch_sandbox_total ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_sandbox_counter_fires_with_none_on_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path -> ``bypass_source='none'``, ``policy='required'``."""
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
        classifier_output=None,
    )

    counter.labels.assert_called_once_with(
        feature="triage", policy="required", bypass_source="none"
    )
    counter.labels.return_value.inc.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_sandbox_counter_fires_with_static_on_no_sandbox_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Label-flip routes ship with ``sandbox_policy=NO_SANDBOX``. We
    want the metric to attribute the bypass to ``static`` so dashboards
    can separate "router skipped" from "classifier skipped"."""
    counter = _record_counter(monkeypatch, "dispatch_sandbox_total")

    async def handler(ctx: PreflightContext) -> None:
        pass

    await execute_handler(
        adapter=AsyncMock(),
        event=_event(kind=EventKind.ISSUE_LABELED),
        dispatch=Dispatch(
            feature=Feature.TRIAGE,
            task_id="t-1",
            handler=handler,
            sandbox_policy=SandboxPolicy.NO_SANDBOX,
        ),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        classifier_output=None,
    )

    counter.labels.assert_called_once_with(
        feature="triage", policy="no_sandbox", bypass_source="static"
    )


@pytest.mark.asyncio
async def test_dispatch_sandbox_counter_fires_with_classifier_on_dynamic_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat with ``intent='unclear'`` -> OR-merge bypasses -> counter
    attributes the bypass to ``classifier`` (different ops story from
    static). ``policy`` records the *merged* result so dashboards see
    NO_SANDBOX outcomes consistently."""
    counter = _record_counter(monkeypatch, "dispatch_sandbox_total")

    async def handler(ctx: PreflightContext) -> None:
        pass

    await execute_handler(
        adapter=AsyncMock(),
        event=_event(kind=EventKind.ISSUE_COMMENT_CREATED),
        dispatch=Dispatch(feature=Feature.CHAT, task_id="t-1", handler=handler),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        classifier_output=ChatClassifierOutput(
            intent="unclear", needs_clarification=True, scope_hint=None
        ),
    )

    counter.labels.assert_called_once_with(
        feature="chat", policy="no_sandbox", bypass_source="classifier"
    )


@pytest.mark.asyncio
async def test_dispatch_sandbox_counter_fires_with_degrade_on_resolver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver raises -> degrade path -> ``bypass_source='degrade'``.
    Same label applies to token-fetch failures and clone failures --
    they're all "we tried and couldn't"."""
    monkeypatch.setattr(
        "openbot.application.dispatcher.resolve_checkout",
        AsyncMock(side_effect=CheckoutResolutionError("missing clone_url")),
    )
    counter = _record_counter(monkeypatch, "dispatch_sandbox_total")

    sandbox = FakeSandboxLifecycle()

    async def handler(ctx: PreflightContext) -> None:
        pass

    await execute_handler(
        adapter=AsyncMock(),
        event=_event(),
        dispatch=Dispatch(feature=Feature.TRIAGE, task_id="t-1", handler=handler),
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        sandbox_factory=_factory_from_sandbox(sandbox),
        classifier_output=None,
    )

    counter.labels.assert_called_once_with(
        feature="triage", policy="required", bypass_source="degrade"
    )


@pytest.mark.asyncio
async def test_dispatch_sandbox_counter_fires_with_degrade_on_clone_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sandbox.clone`` raises -> counter still fires once (clone failure
    is the most ambiguous degrade: resolver succeeded, token came back,
    sandbox opened -- but the clone broke). Without this metric the only
    signal is a buried warning log."""
    monkeypatch.setattr(
        "openbot.application.dispatcher.resolve_checkout",
        AsyncMock(
            return_value=CheckoutSpec(
                repo_url="https://x/y.git", ref="sha", strategy=CloneStrategy.SHALLOW
            )
        ),
    )
    counter = _record_counter(monkeypatch, "dispatch_sandbox_total")

    class _BrokenSandbox(FakeSandboxLifecycle):
        async def clone(self, **_: Any) -> None:
            raise RuntimeError("network blip")

    sandbox = _BrokenSandbox()
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
        classifier_output=None,
    )

    counter.labels.assert_called_once_with(
        feature="triage", policy="required", bypass_source="degrade"
    )


# -- classifier_error_total ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classifier_error_counter_fires_on_classifier_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``classify_for_dispatch`` swallows every exception (fail-open
    contract). The counter is the only signal ops have that the
    classifier is regressing -- the dispatcher itself never raises."""
    monkeypatch.setattr(
        "openbot.dispatcher.classifier.classify_event",
        AsyncMock(side_effect=RuntimeError("litellm 500")),
    )
    counter = MagicMock()
    monkeypatch.setattr("openbot.dispatcher.classifier.classifier_error_total", counter)

    result = await classify_for_dispatch(event=_event(), feature=Feature.TRIAGE, redis=None)

    assert result is None  # fail-open contract preserved
    counter.labels.assert_called_once_with(feature="triage")
    counter.labels.return_value.inc.assert_called_once()


@pytest.mark.asyncio
async def test_classifier_error_counter_does_not_fire_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter only fires on the exception branch -- a clean classification
    result must not inflate the error counter."""
    from openbot.dispatcher.classifier import TriageClassifierOutput

    monkeypatch.setattr(
        "openbot.dispatcher.classifier.classify_event",
        AsyncMock(
            return_value=TriageClassifierOutput(
                type="bug",
                severity_guess="low",
                has_reproduction_info=True,
                looks_like_spam=False,
            )
        ),
    )
    counter = MagicMock()
    monkeypatch.setattr("openbot.dispatcher.classifier.classifier_error_total", counter)

    result = await classify_for_dispatch(event=_event(), feature=Feature.TRIAGE, redis=None)

    assert result is not None
    counter.labels.assert_not_called()


@pytest.mark.asyncio
async def test_classifier_error_counter_does_not_fire_on_feature_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature.FIX skips the classifier entirely (no LLM call). That's
    not an error -- the counter must stay still."""
    counter = MagicMock()
    monkeypatch.setattr("openbot.dispatcher.classifier.classifier_error_total", counter)

    result = await classify_for_dispatch(event=_event(), feature=Feature.FIX, redis=None)

    assert result is None
    counter.labels.assert_not_called()
