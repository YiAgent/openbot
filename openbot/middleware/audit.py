"""AuditStart middleware — write STARTED before handler entry (chain end).

Spec anchor: docs/_archive/prd-history/openbot-harness-spec.md §3 M3
(chain position "AuditStart, before handler") + §3 M9 (lifecycle audit).
The harness spec is archived; its chain decisions are carried forward
in docs/specs/2026-05-17-webhook-worker-layering-design.md.

Why this exists as a separate middleware (and not just inside
``audit_lifecycle``): when a workflow handler raises *before* entering
the ``async with audit_lifecycle`` context — e.g. an ``ImportError``
on first call, a config-coercion crash, or a 5xx from `ctx.adapter.*`
at the very top — there is no STARTED row recorded anywhere. Post-
mortem then can't distinguish:

  (a) ``preflight passed but the handler never even started`` from
  (b) ``handler started, completed silently, audit logger crashed``.

By writing STARTED at the **end of preflight** (last middleware), we
guarantee one of the two patterns appears in audit_log:

  ▸ STARTED + COMPLETED       → happy path.
  ▸ STARTED + FAILED          → handler raised inside lifecycle.
  ▸ STARTED only              → handler crashed before lifecycle.

To avoid double-writes, this middleware sets
``ctx.cache["audit_started"] = True`` after a successful write;
``openbot.workflows._lifecycle.audit_lifecycle`` reads that flag and
**skips** its own STARTED write when the middleware already did it.

Fall-open semantics: a DB unavailability MUST NOT block all webhooks.
The middleware returns PROCEED + WARNING log; the only cost is one
missing audit row. The rest of the chain has already let the request
through, so denying it here would be inconsistent.
"""

from __future__ import annotations

import logging
from typing import Final

from openbot.middleware.preflight import (
    MiddlewareDecision,
    PreflightContext,
)
from openbot.persistence.models import WorkflowPhase

_logger = logging.getLogger(__name__)

# Cache flag consumed by ``audit_lifecycle`` to suppress its own
# STARTED write. Public-ish: the lifecycle module reads the same
# literal, kept here as the single source of truth.
AUDIT_STARTED_CACHE_KEY: Final = "audit_started"


class AuditStartMiddleware:
    """Final chain step — record the STARTED audit row.

    Always returns PROCEED. A failed DB write logs at WARNING and
    sets nothing on the cache, so ``audit_lifecycle`` will fall
    back to writing STARTED itself (the legacy path, still safe).
    """

    name = "audit_start"

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        if ctx.session_factory is None:
            # No Postgres configured — dev mode. Nothing to write
            # and nothing for the lifecycle helper to suppress.
            return MiddlewareDecision.proceed()

        workflow_value = ctx.dispatch.feature.value
        # Local import keeps this module importable in unit tests
        # that don't pull in SQLAlchemy (mirrors the pattern used
        # by ``preflight._write_block_audit``).
        from openbot.persistence.repository import AuditLogRepo

        try:
            async with ctx.session_factory() as session:
                await AuditLogRepo(session).write(
                    phase=WorkflowPhase.STARTED.value,
                    delivery_id=ctx.event.delivery_id or None,
                    repo=ctx.event.repo or None,
                    actor=ctx.event.actor or None,
                    workflow=workflow_value,
                    outcome=None,
                )
                await session.commit()
        except Exception:
            _logger.exception(
                "audit_start_write_failed",
                extra={
                    "delivery_id": ctx.event.delivery_id,
                    "repo": ctx.event.repo,
                    "workflow": workflow_value,
                },
            )
            # Do NOT set the cache flag — lifecycle helper will
            # retry the STARTED write itself, which is the safe
            # fall-back. If both fail, the audit row is genuinely
            # lost; that's a DB outage we'd see in error logs.
            return MiddlewareDecision.proceed()

        ctx.cache[AUDIT_STARTED_CACHE_KEY] = True
        return MiddlewareDecision.proceed()


__all__ = ["AUDIT_STARTED_CACHE_KEY", "AuditStartMiddleware"]
