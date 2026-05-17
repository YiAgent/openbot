"""Dispatch — runs pre-flight + workflow handler for one webhook.

Shared by:

  - ``openbot.webapp``           in-process fallback when Redis is absent
                                 (dev / unit tests via FastAPI BackgroundTask).
  - ``openbot.queue.worker``     Redis Stream consumer, after deserialization.

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
from typing import TYPE_CHECKING

from openbot.config_repo import load_for_repo
from openbot.middleware import (
    ActorRoleMiddleware,
    BudgetMiddleware,
    CancelCommentMiddleware,
    CancelLabelMiddleware,
    ForkPRGateMiddleware,
    KillSwitchMiddleware,
    MiddlewareResult,
    PreflightContext,
    RateLimitMiddleware,
    run_preflight,
)

if TYPE_CHECKING:
    import redis.asyncio as redis_async
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from openbot.adapters.github import GitHubAdapter
    from openbot.events import UnifiedEvent
    from openbot.router import Dispatch

_logger = logging.getLogger(__name__)


def build_preflight_chain() -> list:
    """The locked input-side chain (harness spec §3 M3, slice C order).

    Returned as a list (not a tuple) because asyncio Protocol consumers
    iterate it once per call and we don't reuse instances across
    requests today. New gates append; reordering needs a spec amendment.
    """
    return [
        KillSwitchMiddleware(),
        CancelLabelMiddleware(),
        CancelCommentMiddleware(),
        ForkPRGateMiddleware(),
        ActorRoleMiddleware(),
        RateLimitMiddleware(),
        BudgetMiddleware(),
    ]


async def run_dispatch(
    *,
    adapter: GitHubAdapter,
    event: UnifiedEvent,
    dispatch: Dispatch,
    session_factory: async_sessionmaker[AsyncSession] | None,
    redis: redis_async.Redis | None,
) -> None:
    """Load config → pre-flight → handler.

    NEVER raises out. Errors are logged; the webhook is already 202'd
    and the worker has already (or will) XACK.
    """
    try:
        config = await load_for_repo(adapter, event)
    except Exception:
        _logger.exception(
            "preflight_config_load_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
        return

    ctx = PreflightContext(
        event=event,
        dispatch=dispatch,
        config=config,
        adapter=adapter,
        session_factory=session_factory,
        redis=redis,
    )

    try:
        decision = await run_preflight(ctx, build_preflight_chain())
    except Exception:
        _logger.exception(
            "preflight_runner_crashed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
        return

    if decision.result is not MiddlewareResult.PROCEED:
        # Audit + comment already handled by run_preflight.
        return

    try:
        await dispatch.handler(ctx)
    except Exception:
        _logger.exception(
            "workflow_handler_crashed",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "feature": dispatch.feature.value,
            },
        )


__all__ = ["build_preflight_chain", "run_dispatch"]
