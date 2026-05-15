"""PR review workflow — PRD §4.2 entry stub.

v0.1 Week 2 (slice A): ACK reply + audit. The real LLM-driven review
(diff fetch, finding extraction, severity filter, multi-turn) lands
once the agent slice ships.

Slice A guarantees:
  - Receives a `PreflightContext` that already passed pre-flight
    (Router dispatched it; gates ran).
  - Writes STARTED → COMPLETED|FAILED rows to `audit_log` via the shared
    `audit_lifecycle` helper so all four workflows look identical.
  - Never raises out — background-task failures must not surface as 5xx
    on the webhook (GitHub already got its 202).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openbot.persistence.models import Workflow
from openbot.workflows._lifecycle import audit_lifecycle

if TYPE_CHECKING:
    from openbot.middleware.preflight import PreflightContext

_logger = logging.getLogger(__name__)

_ACK_TEMPLATE = (
    ":robot: OpenBot received this PR and review is queued.\n\n"
    "_v0.1 alpha: only the ACK is automated so far. "
    "Full review (diff scan, structured findings, severity filter) lands in "
    "an upcoming commit._"
)


async def maybe_run_review(ctx: PreflightContext) -> None:
    """Post the PR-review ACK + audit-log the run.

    No-op if `pr_number` is missing — Router already enforces this for
    the dispatch path, the check here is defense-in-depth for callers
    that bypass dispatch (e.g. backfill scripts in v0.2).
    """
    event = ctx.event
    if event.pr_number is None or event.installation_id is None:
        _logger.info(
            "review_skipped_missing_context",
            extra={"delivery_id": event.delivery_id, "kind": event.kind.value},
        )
        return

    message = _ACK_TEMPLATE
    try:
        async with audit_lifecycle(ctx, workflow=Workflow.REVIEW) as audit:
            result = await ctx.adapter.reply(event, message)
            audit.outcome = f"comment_id={result.get('id')}"
            _logger.info(
                "review_ack_posted",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    "pr_number": event.pr_number,
                    "comment_id": result.get("id"),
                },
            )
    except Exception:
        # audit_lifecycle already wrote FAILED. Swallow so the background
        # task doesn't surface as 5xx — GitHub already got its 202.
        _logger.exception(
            "review_ack_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
