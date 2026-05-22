"""Preflight chain and workflow handler execution.

``execute_handler`` is the entry point for worker v3 TaskSpec processing:
it receives a ``PreflightContext`` (already built with ``EffectiveConfig``,
``classifier_output``, etc.) and runs the locked middleware-free path
— sandbox provisioning + handler invocation.

The middleware list lives here (not in webapp) so the worker and the
webapp can't drift apart on the chain order — a single source of
truth per spec §3 M3.

Slice-C note (sandbox DI): ``execute_handler`` accepts a
``sandbox_factory`` kwarg (default ``None``) so the fix use case can
open a sandbox per event. Production callers
(``openbot.infrastructure.queue.worker``, ``openbot.dispatcher.decide``)
still pass ``None`` — wiring a real ``DaytonaSandboxAdapter``-backed
factory at those call sites is the follow-up operational slice. Until
then the fix use case posts a graceful "sandbox not configured" comment.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sys as _sys
import time
from typing import TYPE_CHECKING, Any

from openbot.application.checkout_resolver import resolve_checkout
from openbot.application.middleware import (
    ActorRoleMiddleware,
    AuditStartMiddleware,
    BudgetMiddleware,
    CancelCommentMiddleware,
    CancelLabelMiddleware,
    FeatureToggleMiddleware,
    ForkPRGateMiddleware,
    KillSwitchMiddleware,
    PreflightContext,
    RateLimitMiddleware,
    SanitizeInputsMiddleware,
)
from openbot.application.router import SandboxPolicy
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.core.metrics import (
    dispatch_sandbox_total,
    sandbox_cache_acquire_seconds,
    sandbox_cache_publish_total,
    sandbox_cache_total,
)
from openbot.domain.checkout import CheckoutResolutionError
from openbot.domain.workflows import Workflow

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    import redis.asyncio as redis_async
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from openbot.application.ports.audit_log import AuditLogPort
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.application.ports.rate_limiter import RateLimiterPort
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.application.ports.sandbox_cache import SandboxCachePort
    from openbot.application.router import Dispatch
    from openbot.dispatcher.classifier import ClassifierOutput
    from openbot.domain.config_schema import EffectiveConfig
    from openbot.domain.events import UnifiedEvent

_logger = logging.getLogger(__name__)

# Module-level set keeps strong references to fire-and-forget tasks so
# the garbage collector cannot discard them before they complete (RUF006).
# The ``discard`` done-callback removes each task automatically on exit.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _schedule_background(coro: Any) -> None:
    """Schedule ``coro`` as a fire-and-forget task with a GC-safe reference.

    The task is added to ``_BACKGROUND_TASKS`` and removes itself when
    done. Use this instead of bare ``asyncio.create_task`` anywhere in
    this module.
    """
    task: asyncio.Task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _emit_sandbox_metric(*, feature: str, policy: SandboxPolicy, bypass_source: str) -> None:
    """Fire ``openbot_dispatch_sandbox_total`` exactly once per dispatch.

    Each call site in ``_run_with_sandbox`` invokes this with the
    finalised ``policy`` + ``bypass_source`` pair just before / after
    the handler runs (we always emit, even on the happy path, so
    dashboards can compute ratios without joining on absence).

    Looked up via ``_sys.modules`` on every call so tests that
    monkeypatch ``openbot.application.dispatcher.dispatch_sandbox_total``
    observe the substitution.
    """
    _shim = _sys.modules.get("openbot.application.dispatcher")
    counter = (
        getattr(_shim, "dispatch_sandbox_total", None) if _shim is not None else None
    ) or dispatch_sandbox_total
    counter.labels(feature=feature, policy=policy.value, bypass_source=bypass_source).inc()


def _get_cache_publish_counter() -> Any:
    """Return the monkeypatch-aware ``sandbox_cache_publish_total`` symbol.

    Same lookup pattern as ``_emit_sandbox_metric``; lets tests replace
    the counter at the dispatcher module boundary without touching the
    metrics registry.
    """
    _shim = _sys.modules.get("openbot.application.dispatcher")
    return (
        getattr(_shim, "sandbox_cache_publish_total", None) if _shim is not None else None
    ) or sandbox_cache_publish_total


def _get_cache_total_counter() -> Any:
    """Return the monkeypatch-aware ``sandbox_cache_total`` symbol."""
    _shim = _sys.modules.get("openbot.application.dispatcher")
    return (
        getattr(_shim, "sandbox_cache_total", None) if _shim is not None else None
    ) or sandbox_cache_total


def _get_cache_acquire_seconds() -> Any:
    """Return the monkeypatch-aware ``sandbox_cache_acquire_seconds`` symbol."""
    _shim = _sys.modules.get("openbot.application.dispatcher")
    return (
        getattr(_shim, "sandbox_cache_acquire_seconds", None) if _shim is not None else None
    ) or sandbox_cache_acquire_seconds


async def _safe_publish(
    cache: SandboxCachePort,
    handle: SandboxedHandle,
    installation_id: int,
    feature_value: str,
) -> None:
    """Publish ``handle`` to the cache; swallows all exceptions.

    Called via ``asyncio.create_task`` on the cold path after the handler
    returns (or raises). A publish failure must never surface to the
    webhook layer — it's an optimisation, not a correctness requirement.
    """
    try:
        await cache.publish(handle, installation_id=installation_id)
        _get_cache_publish_counter().labels(feature=feature_value, result="created").inc()
    except Exception:
        _logger.warning(
            "sandbox_cache_publish_failed",
            extra={"feature": feature_value, "installation_id": installation_id},
            exc_info=True,
        )
        _get_cache_publish_counter().labels(feature=feature_value, result="failed").inc()


async def _run_with_sandbox(ctx: PreflightContext) -> None:
    """OR-merge policy gate → resolve → clone → handler.

    Single source of truth for the unified-sandbox-entry contract:

      1. Compute ``effective_policy = derive_sandbox_policy(...)``.
      2. If NO_SANDBOX (static OR classifier said skip) OR the deployment
         has no ``sandbox_factory`` wired, call the handler with
         ``ctx.sandbox_handle is None``. The handler is responsible for
         its workflow-specific degrade reply (e.g. fix posts
         ``_NO_SANDBOX``; chat clarification skips code grounding).
      3. Otherwise ``resolve_checkout`` (which may raise
         ``CheckoutResolutionError`` for unmatched (event, workflow)
         pairs) + ``adapter.get_installation_token`` (which may raise on
         auth issues). Both failures degrade to "no handle" — better to
         post a useful comment than retry forever.
      4. ``async with sandbox_factory() as sandbox`` then ``sandbox.clone``.
         Clone failure also degrades. Note: we don't keep the sandbox
         open after a clone failure — that workspace would be unusable.
      5. Build the ``SandboxedHandle`` triple and call the handler
         *inside* the ``async with`` block so the sandbox stays alive
         for the handler's lifetime; ``close`` runs on context exit.

    The handler is called exactly once on every path. Never raises out
    — ``execute_handler`` catches its own handler exceptions for
    check_run plumbing.
    """
    dispatch = ctx.dispatch
    event = ctx.event

    effective_policy = derive_sandbox_policy(
        static=dispatch.sandbox_policy,
        classifier_output=ctx.classifier_output,
        feature=dispatch.feature,
    )
    if effective_policy is SandboxPolicy.NO_SANDBOX or ctx.sandbox_factory is None:
        # NO_SANDBOX path: handler runs immediately with ``sandbox_handle
        # is None``. ``ctx.classifier_output`` is already in place — the
        # caller set it before invoking us.
        #
        # ``bypass_source`` distinguishes three production-relevant
        # cases: ``static`` (router shipped NO_SANDBOX), ``classifier``
        # (OR-merge bypassed at runtime), ``degrade`` (factory missing —
        # the deployment never wired a sandbox backend). Ops dashboards
        # need to tell these apart because they imply different fixes.
        #
        # Precedence is *cause*-ordered: a static NO_SANDBOX trumps the
        # classifier (router already decided), and a classifier-driven
        # bypass trumps a missing factory (the dispatch was going to
        # skip the sandbox anyway). ``degrade`` only fires when
        # ``effective_policy`` is REQUIRED but the deployment has no
        # factory wired — that's the case ops needs to act on.
        if dispatch.sandbox_policy is SandboxPolicy.NO_SANDBOX:
            bypass_source = "static"
        elif effective_policy is SandboxPolicy.NO_SANDBOX:
            bypass_source = "classifier"
        else:
            bypass_source = "degrade"
        _emit_sandbox_metric(
            feature=dispatch.feature.value,
            policy=effective_policy,
            bypass_source=bypass_source,
        )
        await dispatch.handler(ctx)
        return

    # Tests patch this at the module-attribute level so monkey-patching
    # ``openbot.application.dispatcher.resolve_checkout`` wins over the
    # import-time binding (same pattern as ``load_for_repo``).
    _shim = _sys.modules.get("openbot.application.dispatcher")
    _resolver = (
        getattr(_shim, "resolve_checkout", None) if _shim is not None else None
    ) or resolve_checkout
    try:
        # Workflow vs Feature: same string values, different identity
        # types (audit identity vs LLM-routing identity). The resolver
        # is audit-side, so we coerce here at the boundary.
        checkout = await _resolver(event, Workflow(dispatch.feature.value), ctx.adapter)
        token = await ctx.adapter.get_installation_token(event)
    except CheckoutResolutionError as exc:
        _logger.warning(
            "sandbox_provisioning_skipped_resolution",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "feature": dispatch.feature.value,
                "error": str(exc),
            },
        )
        _emit_sandbox_metric(
            feature=dispatch.feature.value, policy=effective_policy, bypass_source="degrade"
        )
        await dispatch.handler(ctx)
        return
    except Exception:
        _logger.exception(
            "sandbox_provisioning_skipped_token",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "feature": dispatch.feature.value,
            },
        )
        _emit_sandbox_metric(
            feature=dispatch.feature.value, policy=effective_policy, bypass_source="degrade"
        )
        await dispatch.handler(ctx)
        return

    factory = ctx.sandbox_factory
    cache = ctx.sandbox_cache
    # Capture once so the None-narrowing flows through both the cache
    # branch and the cold-path ``finally`` without repeating the check.
    installation_id: int | None = event.installation_id

    # ── Warm-cache branch ──────────────────────────────────────────────
    # Try SandboxCachePort.acquire before opening the factory. On a hit
    # the handler runs immediately with the cached handle and we return
    # early, skipping the cold clone entirely. On miss/error we fall
    # through to the existing factory path unchanged.
    #
    # Guard ``installation_id is not None``: the SandboxCachePort
    # protocol requires a concrete int for the key derivation; events
    # without an installation_id (e.g. public-repo triggers without an
    # App install) bypass the cache and always run the cold path.
    if cache is not None and installation_id is not None:
        _cache_start = time.perf_counter()
        try:
            cached = await cache.acquire(checkout, token, installation_id=installation_id)
        except Exception as _exc:
            _logger.warning(
                "sandbox_cache_acquire_failed",
                extra={"feature": dispatch.feature.value, "err": str(_exc)},
            )
            _get_cache_total_counter().labels(
                feature=dispatch.feature.value, result="backend_error"
            ).inc()
            cached = None
        else:
            if cached is None:
                _get_cache_total_counter().labels(
                    feature=dispatch.feature.value, result="miss"
                ).inc()

        if cached is not None:
            _get_cache_total_counter().labels(feature=dispatch.feature.value, result="hit").inc()
            _get_cache_acquire_seconds().labels(result="hit").observe(
                time.perf_counter() - _cache_start
            )
            _emit_sandbox_metric(
                feature=dispatch.feature.value,
                policy=effective_policy,
                bypass_source="none",
            )
            ctx_with_handle = dataclasses.replace(ctx, sandbox_handle=cached)
            await dispatch.handler(ctx_with_handle)
            return

    # ── Cold path — open factory and clone ────────────────────────────
    try:
        async with factory() as sandbox:
            try:
                await sandbox.clone(
                    repo_url=checkout.repo_url,
                    ref=checkout.ref,
                    token=token,
                    strategy=checkout.strategy,
                )
            except Exception:
                _logger.warning(
                    "sandbox_clone_failed",
                    extra={
                        "delivery_id": event.delivery_id,
                        "repo": event.repo,
                        "feature": dispatch.feature.value,
                        "ref": checkout.ref,
                    },
                    exc_info=True,
                )
                _emit_sandbox_metric(
                    feature=dispatch.feature.value,
                    policy=effective_policy,
                    bypass_source="degrade",
                )
                await dispatch.handler(ctx)
                return
            # Happy path — sandbox + clone both succeeded. Emit ``none``
            # so the dashboard's "sandbox open" rate is computable.
            _emit_sandbox_metric(
                feature=dispatch.feature.value,
                policy=effective_policy,
                bypass_source="none",
            )
            cold_handle = SandboxedHandle(sandbox=sandbox, checkout=checkout, token=token)
            ctx_with_handle = dataclasses.replace(ctx, sandbox_handle=cold_handle)
            try:
                await dispatch.handler(ctx_with_handle)
            finally:
                # Schedule publish regardless of handler outcome so the
                # warm pool is populated for the next request. Failure in
                # the handler is the handler's concern; the cache layer
                # should not miss a publish opportunity because of it.
                if cache is not None and installation_id is not None:
                    _schedule_background(
                        _safe_publish(
                            cache,
                            cold_handle,
                            installation_id,
                            dispatch.feature.value,
                        )
                    )
    except Exception:
        # Factory itself blew up (connection failure to remote backend,
        # quota error, etc.). Last-ditch: call the handler without a
        # sandbox so it can at least post a "sandbox unavailable" reply.
        _logger.exception(
            "sandbox_factory_failed",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "feature": dispatch.feature.value,
            },
        )
        _emit_sandbox_metric(
            feature=dispatch.feature.value, policy=effective_policy, bypass_source="degrade"
        )
        await dispatch.handler(ctx)


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
    sandbox_cache: SandboxCachePort | None = None,
    classifier_output: ClassifierOutput | None = None,
    agent_checkpointer: BaseCheckpointSaver | None = None,
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
        # Slice-C DI: the TaskSpec v3 path also needs the sandbox factory
        # so worker-side FIX dispatches can run the loop end-to-end.
        sandbox_factory=sandbox_factory,
        sandbox_cache=sandbox_cache,
        classifier_output=classifier_output,
        agent_checkpointer=agent_checkpointer,
    )
    try:
        # Same provisioning block as ``run_dispatch``. The worker has
        # already rehydrated ``classifier_output`` from the TaskSpec
        # (so we don't re-classify), but the policy gate + clone still
        # apply here — otherwise the worker path would always run with
        # ``sandbox_handle is None`` and break the unified-entry contract.
        await _run_with_sandbox(ctx)
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
from openbot.application.sandbox_policy import derive_sandbox_policy  # noqa: E402
from openbot.dispatcher.classifier import classify_for_dispatch  # noqa: E402

__all__ = [
    "build_preflight_chain",
    "classify_for_dispatch",
    "derive_sandbox_policy",
    "execute_handler",
]
