# openbot/dispatcher/decide.py
"""decide_and_enqueue — webhook async segment.

Runs the full preflight chain on the webhook async path, then builds a
TaskSpec v3 and enqueues it. The worker skips preflight and goes straight
to the handler. Falls back to in-process execution when no queue is
configured (dev / unit-test mode).

Never raises out — callers (BackgroundTask, tests) expect fire-and-forget.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict as _dataclass_asdict
from typing import TYPE_CHECKING

from openbot.application.dispatcher import build_preflight_chain, execute_handler
from openbot.application.middleware import MiddlewareResult, PreflightContext, run_preflight
from openbot.application.state.runs_repo import get_last_reviewed_sha
from openbot.dispatcher.classifier import classify_event, stages_from_classifier
from openbot.dispatcher.context import extract_event_context
from openbot.dispatcher.direct_actions import RULES_BY_FEATURE, DirectAction
from openbot.dispatcher.incremental import compute_diff_scope
from openbot.domain.workflows import Feature
from openbot.infrastructure.persistence.db import session_scope
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


def _extract_initial_labels(raw: dict[str, object]) -> list[str]:
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


async def _send_direct_action(
    adapter: ChannelAdapterPort,
    event: UnifiedEvent,
    action: DirectAction,
    *,
    feature: Feature,
) -> None:
    """Post a canned reply (and optional labels) for a direct-action short-circuit.

    Reply and label calls go to the same backend (GitHub) and are independent,
    so we run them concurrently. Any failure is logged but not re-raised — the
    short-circuit should never break the webhook contract.
    """
    try:
        coros: list = [adapter.reply(event, action.message)]
        if action.labels_to_add:
            coros.append(adapter.add_label(event, *action.labels_to_add))
        await asyncio.gather(*coros)
        _logger.info(
            "decide_and_enqueue_direct_action",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "feature": str(feature),
                "labels_added": list(action.labels_to_add),
            },
        )
    except Exception:
        _logger.exception(
            "direct_action_reply_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )


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
    """Webhook async segment: run preflight, build TaskSpec v3, enqueue.

    On PROCEED with a queue available: builds TaskSpec v3 and XADD's it.
    On PROCEED without a queue: calls execute_handler() in-process (dev fallback).
    On BLOCKED: returns silently (middleware already wrote the reply).

    Never raises out.
    """
    try:
        # Load effective config for this repo.
        if config_loader is not None:
            config = await config_loader.load_for_repo(adapter, event)
        else:
            from openbot.infrastructure.config_loader import load_for_repo

            config = await load_for_repo(adapter, event)

        # Run the preflight chain (same chain the worker used to run).
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

        # Extract structured context from raw payload (pure, no I/O).
        ev_ctx = extract_event_context(event)
        feature = dispatch.feature
        rule = RULES_BY_FEATURE.get(feature)
        direct_action = rule(ev_ctx) if rule is not None else None

        # Direct-action short-circuit — reply and return without enqueuing.
        if direct_action is not None:
            await _send_direct_action(adapter, event, direct_action, feature=feature)
            return

        initial_labels = _extract_initial_labels(event.raw)

        # Classify event (one-shot LLM) — fail-open: on exception, treat as
        # classifier_skipped and let the worker run all stages.
        classifier_result = None
        if feature is not Feature.FIX:
            try:
                classifier_result = await classify_event(
                    feature=feature,
                    body=ev_ctx.classification_body,
                    redis=redis,
                )
            except Exception:
                _logger.exception(
                    "classifier_exception_in_decide",
                    extra={"delivery_id": event.delivery_id, "repo": event.repo},
                )
        classifier_output = (
            _dataclass_asdict(classifier_result) if classifier_result is not None else None
        )
        stages = stages_from_classifier(feature, classifier_result)

        # For PR review events, fetch the SHA from the last completed review
        # so DiffScope can decide if this push is incremental. Use
        # event.resource_key (always computed from channel:repo:pr:N) rather
        # than dispatch.resource_key (only populated on the state-machine
        # receive path, None in unit tests and dev mode).
        is_incremental = False
        is_force_push = False
        if feature is Feature.REVIEW:
            last_reviewed_sha: str | None = None
            if session_factory is not None and event.resource_key is not None:
                async with session_scope(session_factory) as _session:
                    last_reviewed_sha = await get_last_reviewed_sha(_session, event.resource_key)
            diff_scope = compute_diff_scope(event.raw, last_reviewed_sha=last_reviewed_sha)
            is_incremental = diff_scope.is_incremental
            is_force_push = diff_scope.is_force_push

        if queue is not None:
            spec = TaskSpec.from_event_and_dispatch(
                event,
                dispatch,
                check_run_id=check_run_id,
                decision_trace=[],
                initial_labels=initial_labels,
                classifier_output=classifier_output,
                stages_to_run=stages,
                is_incremental=is_incremental,
                is_force_push=is_force_push,
            )
            await queue.enqueue_task_spec(spec)
            _logger.info(
                "decide_and_enqueue_queued",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    "scenario": spec.scenario,
                    "task_id": spec.task_id,
                    "classifier_skipped": spec.classifier_skipped,
                    "stages_to_run": spec.stages_to_run,
                    "is_incremental": spec.is_incremental,
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
