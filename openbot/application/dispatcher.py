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
"""

from __future__ import annotations

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
    import redis.asyncio as redis_async
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from openbot.application.ports.audit_log import AuditLogPort
    from openbot.application.ports.config_loader import ConfigLoaderPort
    from openbot.application.ports.rate_limiter import RateLimiterPort
    from openbot.application.router import Dispatch
    from openbot.domain.events import UnifiedEvent
    from openbot.infrastructure.adapters.github import GitHubAdapter

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
    adapter: GitHubAdapter,
    event: UnifiedEvent,
    dispatch: Dispatch,
    session_factory: async_sessionmaker[AsyncSession] | None,
    redis: redis_async.Redis | None,
    check_run_id: int | None = None,
    audit: AuditLogPort | None = None,
    rate_limiter: RateLimiterPort | None = None,
    config_loader: ConfigLoaderPort | None = None,
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


__all__ = ["build_preflight_chain", "run_dispatch"]
