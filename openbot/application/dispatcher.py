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
    MiddlewareResult,
    PreflightContext,
    RateLimitMiddleware,
    SanitizeInputsMiddleware,
    run_preflight,
)
from openbot.application.router import SandboxPolicy
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.core.metrics import dispatch_sandbox_total
from openbot.domain.checkout import CheckoutResolutionError
from openbot.domain.workflows import Workflow
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
    — callers (``run_dispatch`` / ``execute_handler``) catch their own
    handler exceptions for check_run plumbing.
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
            ctx_with_handle = dataclasses.replace(
                ctx,
                sandbox_handle=SandboxedHandle(sandbox=sandbox, checkout=checkout, token=token),
            )
            await dispatch.handler(ctx_with_handle)
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
        # Sandbox provisioning lives in ``_run_with_sandbox`` — it
        # OR-merges the static dispatch policy with the classifier
        # signal, optionally opens a sandbox + clone, and calls the
        # handler exactly once. We still wrap in try/except here so the
        # check_run success/failure plumbing fires symmetrically on
        # both the sandboxed and the bypass paths.
        await _run_with_sandbox(ctx)
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
    "run_dispatch",
]
