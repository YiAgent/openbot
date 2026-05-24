"""Workflow lifecycle audit helpers — harness spec §3 M9.

All four workflow stubs share the same STARTED → (COMPLETED | FAILED)
audit pattern. Centralised here so:

  - The exact audit_log shape (delivery_id / repo / actor / workflow /
    phase / outcome) stays consistent across triage/review/fix/chat.
  - Failure handling is uniform: an audit write that itself raises is
    logged at ERROR but never propagates out of the workflow — losing
    an audit row beats crashing the workflow that produced it.

`audit_lifecycle` is an async context manager:

    async with audit_lifecycle(ctx, workflow=Workflow.TRIAGE) as audit:
        result = await ctx.adapter.reply(ctx.event, message)
        audit.outcome = f"comment_id={result.get('id')}"

On exit-without-exception → COMPLETED row (uses `audit.outcome` if set).
On exit-with-exception     → FAILED row (uses str(exc)) + exception re-raised.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openbot.core.metrics import workflow_total
from openbot.domain.workflows import Workflow, WorkflowPhase

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from openbot.application.middleware.preflight import PreflightContext
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.domain.events import UnifiedEvent

_logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Sticky-reply helper
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _StickyReply:
    """Handle returned by :func:`sticky_reply`.

    Call :meth:`update` to replace the placeholder comment body in-place.
    If the initial POST failed (e.g. auth not wired), ``comment_id`` is
    ``None`` and ``update`` is a silent no-op so callers never have to
    branch on it.

    ``fallback_on_update_error`` (default ``False``) opts the caller into
    *louder degradation* — when set, an update that has no placeholder
    to patch, or whose PATCH raises, posts a fresh comment so the message
    still lands. Even the fallback ``reply()`` keeps the silent-failure
    contract: if it also raises, we log and return. Used by the reproduce
    responder where losing the outcome row is worse than double-posting.
    """

    comment_id: int | None
    _adapter: Any
    _event: Any
    fallback_on_update_error: bool = False

    async def update(self, message: str) -> None:
        """Replace the comment body via PATCH; optionally fall back to a new comment."""
        if self.comment_id is not None:
            try:
                await self._adapter.update_comment(self._event, self.comment_id, message)
                return
            except Exception:
                _logger.exception(
                    "sticky_reply_update_failed",
                    extra={"comment_id": self.comment_id},
                )
                if not self.fallback_on_update_error:
                    return
        elif not self.fallback_on_update_error:
            # No placeholder + no fallback opt-in → behaviour pre-flag: no-op.
            return
        # Either the initial POST failed (comment_id is None) or PATCH raised
        # — in both cases the caller asked for a fallback, so try a fresh
        # comment. A failure here is still swallowed: losing the audit row
        # beats crashing the workflow body that produced it.
        try:
            await self._adapter.reply(self._event, message)
        except Exception:
            _logger.exception(
                "sticky_reply_fallback_failed",
                extra={"repo": getattr(self._event, "repo", None)},
            )


@asynccontextmanager
async def sticky_reply(
    adapter: ChannelAdapterPort,
    event: UnifiedEvent,
    *,
    initial: str,
    fallback_on_update_error: bool = False,
) -> AsyncGenerator[_StickyReply, None]:
    """Post a placeholder comment; yield a handle to update it in-place.

    Usage::

        async with sticky_reply(adapter, event, initial="⏳ Working…") as sticky:
            result = await do_heavy_work()
            await sticky.update(f"✅ Done: {result}")

    The initial POST runs outside the caller's try/except so a GitHub API
    outage at startup doesn't silently skip the placeholder — failures are
    logged and ``comment_id`` is set to ``None``.  Subsequent
    :meth:`_StickyReply.update` calls are then no-ops so the caller's logic
    continues without having to branch.

    Pass ``fallback_on_update_error=True`` when the outcome message MUST
    reach the channel even if the sticky path is broken (e.g. reproduce
    responder); see :class:`_StickyReply` for the exact semantics.
    """
    comment_id: int | None = None
    try:
        result = await adapter.reply(event, initial)
        comment_id = result.get("id")
    except Exception:
        _logger.exception(
            "sticky_reply_initial_failed",
            extra={"repo": event.repo},
        )
    yield _StickyReply(
        comment_id=comment_id,
        _adapter=adapter,
        _event=event,
        fallback_on_update_error=fallback_on_update_error,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Audit-lifecycle helper
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _AuditHandle:
    """Mutable thunk the `with`-body fills in before COMPLETED is written."""

    outcome: str | None = None


async def _write_phase(
    ctx: PreflightContext,
    *,
    workflow: Workflow,
    phase: WorkflowPhase,
    outcome: str | None,
) -> None:
    if ctx.session_factory is None:
        return
    from openbot.infrastructure.persistence.repository import AuditLogRepo

    try:
        async with ctx.session_factory() as session:
            await AuditLogRepo(session).write(
                phase=phase.value,
                delivery_id=ctx.event.delivery_id or None,
                repo=ctx.event.repo or None,
                actor=ctx.event.actor or None,
                workflow=workflow.value,
                outcome=outcome,
            )
            await session.commit()
    except Exception:
        _logger.exception(
            "workflow_audit_write_failed",
            extra={
                "workflow": workflow.value,
                "phase": phase.value,
                "delivery_id": ctx.event.delivery_id,
                "repo": ctx.event.repo,
            },
        )


@asynccontextmanager
async def audit_lifecycle(
    ctx: PreflightContext,
    *,
    workflow: Workflow,
) -> AsyncGenerator[_AuditHandle, None]:
    """Wrap a workflow body with STARTED / COMPLETED / FAILED rows.

    The handle's `outcome` is written on the COMPLETED row. Exceptions
    are re-raised after the FAILED row is written, so callers can decide
    whether to swallow them (workflow stubs do — see each stub).

    Coordination with ``AuditStartMiddleware`` (spec §3 M3 + §3 M9):
    when the middleware already wrote STARTED at the end of preflight,
    it sets ``ctx.cache[AUDIT_STARTED_CACHE_KEY] = True``. We skip our
    own STARTED write in that case so there's exactly one STARTED row
    per webhook delivery. The middleware path is preferred because it
    catches handler-side import / crash before the ``async with`` body
    even runs; the in-context fallback here exists for tests + dev
    modes that bypass the chain.
    """
    # Import locally to avoid an import cycle: openbot.application.middleware.audit
    # already imports from preflight, which imports models — a top-level
    # import here would pull middleware into workflows. The cache key
    # is a single constant string so the cost is trivial.
    from openbot.application.middleware.audit import AUDIT_STARTED_CACHE_KEY

    handle = _AuditHandle()
    if not ctx.cache.get(AUDIT_STARTED_CACHE_KEY):
        await _write_phase(ctx, workflow=workflow, phase=WorkflowPhase.STARTED, outcome=None)
    try:
        yield handle
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            # RunCancelledError is a subclass of asyncio.CancelledError which
            # is BaseException, NOT Exception.  Without this branch the audit
            # row stays in STARTED forever on cancellation.  Write CANCELLED
            # before re-raising so lifecycle rows always reach a terminal state.
            await _write_phase(
                ctx,
                workflow=workflow,
                phase=WorkflowPhase.CANCELLED,
                outcome="RunCancelledError",
            )
            workflow_total.labels(feature=workflow.value, outcome="cancelled").inc()
        else:
            # Keep the FAILED outcome short and PII-free — the body of any
            # GitHub API error can leak repo paths or login names.
            outcome = f"{type(exc).__name__}"
            await _write_phase(ctx, workflow=workflow, phase=WorkflowPhase.FAILED, outcome=outcome)
            workflow_total.labels(feature=workflow.value, outcome="failed").inc()
        raise
    await _write_phase(
        ctx, workflow=workflow, phase=WorkflowPhase.COMPLETED, outcome=handle.outcome
    )
    workflow_total.labels(feature=workflow.value, outcome="completed").inc()
