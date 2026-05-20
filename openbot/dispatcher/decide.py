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
                lbl.get("name", "") for lbl in labels if isinstance(lbl, dict) and lbl.get("name")
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
            config = await config_loader.load_for_repo(adapter, event)
        else:
            from openbot.infrastructure.config_loader import load_for_repo

            config = await load_for_repo(adapter, event)

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
        decision = await run_preflight(ctx, chain)

        if decision.result is not MiddlewareResult.PROCEED:
            return

        initial_labels = _extract_initial_labels(event.raw)

        if queue is not None:
            spec = TaskSpec.from_event_and_dispatch(
                event,
                dispatch,
                check_run_id=check_run_id,
                decision_trace=[],
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
