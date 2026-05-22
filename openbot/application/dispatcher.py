"""Dispatch — runs pre-flight + workflow handler for one webhook.

Shared by:

  - ``openbot.entrypoints.api.app``           in-process fallback when Redis is absent
                                 (dev / unit tests via FastAPI BackgroundTask).
  - ``openbot.infrastructure.queue.worker``     Redis Stream consumer, after deserialization.

Both paths arrive at this module with the same five inputs:
adapter, event, dispatch decision, session-factory handle, Redis handle.
The function loads ``EffectiveConfig``, builds a ``PreflightContext``,
runs the locked middleware chain, and (on PROCEED) invokes the workflow
handler. Never raises out — the caller has already 202'd / XACK'd by
the time this returns.

The middleware list lives here (not in webapp) so the worker and the
webapp can't drift apart on the chain order — a single source of
truth per spec §3 M3.

Slice-C note (sandbox DI): both ``run_dispatch`` and ``execute_handler``
accept a ``sandbox_factory`` kwarg (default ``None``) so the fix use
case can open a sandbox per event. Production callers
(``openbot.entrypoints.api.app``, ``openbot.infrastructure.queue.worker``,
``openbot.dispatcher.decide``) still pass ``None`` — wiring a real
``DaytonaSandboxAdapter``-backed factory at those call sites is the
follow-up operational slice. Until then the fix use case posts a
graceful "sandbox not configured" comment.
"""

from __future__ import annotations

import dataclasses
import logging
import sys as _sys
from typing import TYPE_CHECKING

from openbot.application.middleware import (
    ActorRoleMiddleware,
    AuditStartMiddleware,
    BudgetMiddleware,
    CancelCommentMiddleware,
    CancelLabelMiddleware,
    FeatureToggleMiddleware,
    ForkPRGateMiddleware,
    KillSwitchMiddleware,
    MiddlewareResult,
    PreflightContext,
    RateLimitMiddleware,
    SanitizeInputsMiddleware,
    run_preflight,
)
from openbot.infrastructure.config_loader import load_for_repo

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    import redis.asyncio as redis_async
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from openbot.application.ports.audit_log import AuditLogPort
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.application.ports.config_loader import ConfigLoaderPort
    from openbot.application.ports.rate_limiter import RateLimiterPort
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.application.router import Dispatch
    from openbot.dispatcher.classifier import ClassifierOutput
    from openbot.domain.config_schema import EffectiveConfig
    from openbot.domain.events import UnifiedEvent

_logger = logging.getLogger(__name__)


def build_preflight_chain() -> list:
    """The locked input-side chain (harness spec §3 M3 + plan §5 amendments).

    Order rationale (cheapest first, then sharper teeth, then audit):

      1. ``sanitize_inputs``  byte-level entry gate; must run first so
                              every downstream middleware sees the
                              cleaned event via ``sanitized_event(ctx)``.
      2. ``kill_switch``      env-var check; admin-controlled override
                              that should silence everything below it.
      3. ``feature_toggle``   `.openbot/config.yaml` features.{x}=false
                              gate. Cheaper than a GitHub API call but
                              user-controlled so sits *after* kill switch.
      4. ``cancel_label``     one GitHub API call; result cached on
                              ``ctx.cache`` for downstream reuse.
      5. ``cancel_comment``   regex on comment body; only fires on chat
                              events. Spec §9.4 says always-reply.
      6. ``fork_pr_gate``     PRD §4.8 hard gate; consults same labels
                              cache from ``cancel_label``.
      7. ``actor_role``       per-feature role allow-list (FIX / CHAT).
                              Caches role on ``ctx.cache`` for #8.
      8. ``rate_limit``       Redis counters for chat-feature day/hour.
      9. ``budget``           Postgres ``cost_meter`` sum check.
      10. ``audit_start``     last gate before handler — records the
                              STARTED row so a handler that import-errors
                              still leaves a "preflight passed" trail
                              (spec §3 M9).

    Returned as a list (not a tuple) because asyncio Protocol consumers
    iterate it once per call. Reordering needs a spec amendment + the
    test in ``tests/middleware/test_chain_order.py``.
    """
    return [
        SanitizeInputsMiddleware(),
        KillSwitchMiddleware(),
        FeatureToggleMiddleware(),
        CancelLabelMiddleware(),
        CancelCommentMiddleware(),
        ForkPRGateMiddleware(),
        ActorRoleMiddleware(),
        RateLimitMiddleware(),
        BudgetMiddleware(),
        AuditStartMiddleware(),
    ]


async def run_dispatch(
    *,
    adapter: ChannelAdapterPort,
    event: UnifiedEvent,
    dispatch: Dispatch,
    session_factory: async_sessionmaker[AsyncSession] | None,
    redis: redis_async.Redis | None,
    check_run_id: int | None = None,
    audit: AuditLogPort | None = None,
    rate_limiter: RateLimiterPort | None = None,
    config_loader: ConfigLoaderPort | None = None,
    sandbox_factory: (Callable[[], AbstractAsyncContextManager[SandboxPort]] | None) = None,
) -> None:
    """Load config → pre-flight → handler.

    NEVER raises out. Errors are logged; the webhook is already 202'd
    and the worker has already (or will) XACK.
    """
    try:
        if config_loader is not None:
            config = await config_loader.load_for_repo(adapter, event)
        else:
            # Legacy path: respect monkeypatching for tests that pre-date the Port.
            _dispatch_shim = _sys.modules.get("openbot.application.dispatcher")
            _loader = (
                getattr(_dispatch_shim, "load_for_repo", None)
                if _dispatch_shim is not None
                else None
            ) or load_for_repo
            config = await _loader(adapter, event)
    except Exception:
        _logger.exception(
            "preflight_config_load_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
        if check_run_id:
            try:
                await adapter.update_check_run(
                    event,
                    check_run_id,
                    status="completed",
                    conclusion="failure",
                    output={
                        "title": "Config Load Failed",
                        "summary": "Failed to load `.openbot/config.yaml`. Check repository permissions.",
                    },
                )
            except Exception:
                _logger.exception("check_run_update_failed_on_config_load")
        return

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
        # Slice-C DI: callers (webapp, worker, E2E harness) inject a
        # ``DaytonaSandboxAdapter`` (or test fake) factory. ``None``
        # keeps the fix use case on its graceful "sandbox not configured"
        # branch for deployments that haven't enabled the sandbox.
        sandbox_factory=sandbox_factory,
    )

    try:
        # Respect monkeypatching of ``openbot.application.dispatcher.run_preflight``.
        _dispatch_shim2 = _sys.modules.get("openbot.application.dispatcher")
        _preflight_fn = (
            getattr(_dispatch_shim2, "run_preflight", None) if _dispatch_shim2 is not None else None
        ) or run_preflight
        decision = await _preflight_fn(ctx, build_preflight_chain())
    except Exception:
        _logger.exception(
            "preflight_runner_crashed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
        if check_run_id:
            try:
                await adapter.update_check_run(
                    event,
                    check_run_id,
                    status="completed",
                    conclusion="failure",
                    output={
                        "title": "Pre-flight Crash",
                        "summary": "The pre-flight middleware runner crashed unexpectedly.",
                    },
                )
            except Exception:
                _logger.exception("check_run_update_failed_on_preflight_crash")
        return

    if decision.result is not MiddlewareResult.PROCEED:
        # Audit + comment already handled by run_preflight.
        if check_run_id:
            try:
                await adapter.update_check_run(
                    event,
                    check_run_id,
                    status="completed",
                    conclusion="skipped",
                    output={
                        "title": "Analysis Skipped",
                        "summary": f"Workflow blocked by middleware: `{decision.reason or 'unknown'}`",
                    },
                )
            except Exception:
                _logger.exception("check_run_update_failed_on_blocked")
        return

    # Classifier hook (single source of truth — see ``classify_for_dispatch``).
    # Runs *after* preflight passes so a BLOCKED chain never burns an LLM call.
    # Monkeypatch-friendly indirection mirrors the ``load_for_repo`` /
    # ``run_preflight`` pattern above: tests swap the module attribute.
    _dispatch_shim3 = _sys.modules.get("openbot.application.dispatcher")
    _classify_fn = (
        getattr(_dispatch_shim3, "classify_for_dispatch", None)
        if _dispatch_shim3 is not None
        else None
    ) or classify_for_dispatch
    classifier_output = await _classify_fn(event=event, feature=dispatch.feature, redis=redis)
    ctx = dataclasses.replace(ctx, classifier_output=classifier_output)

    try:
        await dispatch.handler(ctx)
        # Success path: update check_run if present.
        if check_run_id:
            try:
                await adapter.update_check_run(
                    event,
                    check_run_id,
                    status="completed",
                    conclusion="success",
                    output={
                        "title": "Analysis Complete",
                        "summary": f"Workflow `{dispatch.feature.value}` finished successfully.",
                    },
                )
            except Exception:
                _logger.exception("check_run_update_failed_on_success")
    except Exception:
        _logger.exception(
            "workflow_handler_crashed",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "feature": dispatch.feature.value,
            },
        )
        # Failure path: mark the check as failed.
        if check_run_id:
            try:
                await adapter.update_check_run(
                    event,
                    check_run_id,
                    status="completed",
                    conclusion="failure",
                    output={
                        "title": "Handler Crash",
                        "summary": f"The `{dispatch.feature.value}` handler raised an unhandled exception.",
                    },
                )
            except Exception:
                _logger.exception("check_run_update_failed_on_handler_crash")


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
) -> None:
    """Execute workflow handler directly — no preflight.

    Used by the worker when processing a TaskSpec v3: the webhook async
    segment already ran the full preflight chain. Never raises out.

    ``classifier_output`` is supplied by the worker (after
    ``parse_classifier_output``-rehydrating the dict from ``TaskSpec``),
    or by the in-process fallback in ``decide_and_enqueue`` after it
    calls ``classify_for_dispatch`` once. Callers that haven't been
    upgraded yet pass ``None`` and the handler sees ``None`` on
    ``ctx.classifier_output`` — the policy gate degrades to the static
    ``SandboxPolicy``.
    """
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
        # Slice-C DI: same shape as ``run_dispatch`` — the TaskSpec v3
        # path also needs the sandbox factory so worker-side FIX
        # dispatches can run the loop end-to-end.
        sandbox_factory=sandbox_factory,
        classifier_output=classifier_output,
    )
    try:
        await dispatch.handler(ctx)
        if check_run_id:
            try:
                await adapter.update_check_run(
                    event,
                    check_run_id,
                    status="completed",
                    conclusion="success",
                    output={
                        "title": "Analysis Complete",
                        "summary": f"Workflow `{dispatch.feature.value}` finished.",
                    },
                )
            except Exception:
                _logger.exception("check_run_update_failed_on_success")
    except Exception:
        _logger.exception(
            "workflow_handler_crashed",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "feature": dispatch.feature.value,
            },
        )
        if check_run_id:
            try:
                await adapter.update_check_run(
                    event,
                    check_run_id,
                    status="completed",
                    conclusion="failure",
                    output={
                        "title": "Handler Crash",
                        "summary": f"Handler `{dispatch.feature.value}` raised unexpectedly.",
                    },
                )
            except Exception:
                _logger.exception("check_run_update_failed_on_handler_crash")


# Late binding: ``openbot.dispatcher.classifier`` indirectly re-imports
# this module (via ``openbot.dispatcher.__init__`` → ``decide.py``), so a
# top-of-file ``from openbot.dispatcher.classifier import …`` would race
# with ``build_preflight_chain`` not yet being defined. Importing after
# all module-level defs leaves the symbol on this module's namespace —
# which is exactly what test monkeypatches (``setattr(
# 'openbot.application.dispatcher.classify_for_dispatch', …)``) need.
from openbot.dispatcher.classifier import classify_for_dispatch  # noqa: E402

__all__ = [
    "build_preflight_chain",
    "classify_for_dispatch",
    "execute_handler",
    "run_dispatch",
]
