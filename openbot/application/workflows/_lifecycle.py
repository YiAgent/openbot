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

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from openbot.infrastructure.persistence.models import Workflow, WorkflowPhase

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from openbot.application.middleware.preflight import PreflightContext

_logger = logging.getLogger(__name__)


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
    except Exception as exc:
        # Keep the FAILED outcome short and PII-free — the body of any
        # GitHub API error can leak repo paths or login names.
        outcome = f"{type(exc).__name__}"
        await _write_phase(ctx, workflow=workflow, phase=WorkflowPhase.FAILED, outcome=outcome)
        raise
    await _write_phase(
        ctx, workflow=workflow, phase=WorkflowPhase.COMPLETED, outcome=handle.outcome
    )
