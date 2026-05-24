"""Triage workflow — PRD §4.1.

Slice A refactor: now takes a `PreflightContext` (matching the other
three workflow stubs) and writes STARTED/COMPLETED/FAILED audit rows
through the shared `audit_lifecycle` helper.

Behavior unchanged from Week 1:
  - Post an ACK comment on `issues.opened` so the reporter knows
    OpenBot saw the issue.
  - Skip bot-authored issues (PRD §4 echo-loop defense; Router also
    blocks these but defense-in-depth is cheap).
  - Skip when installation_id or issue_number is missing.
  - Never raise — background-task failures must not surface as 5xx.

PRD §4.1's full pipeline (auto-label → reproduce → priority) still
lands in upcoming commits.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openbot.application.use_cases._lifecycle import audit_lifecycle
from openbot.application.use_cases._tracing import observe as _observe
from openbot.application.use_cases._tracing import traceable as _traceable
from openbot.domain.events import EventKind
from openbot.domain.workflows import Workflow

if TYPE_CHECKING:
    from openbot.application.middleware.preflight import PreflightContext

_logger = logging.getLogger("openbot.application.use_cases.triage")

_ACK_TEMPLATE = (
    ":robot: Hi @{actor} — OpenBot received this issue and will triage shortly.\n\n"
    "_v0.1 alpha: only the ACK is automated so far. "
    "Auto-label, priority, and sandbox reproduce land in upcoming commits._"
)


@_observe(name="triage", capture_input=False)
@_traceable(run_type="chain", name="triage")
async def maybe_run_triage(ctx: PreflightContext) -> None:
    event = ctx.event
    if event.kind is not EventKind.ISSUE_OPENED:
        # Defense-in-depth: Router only dispatches ISSUE_OPENED to triage, but
        # any future caller (backfill scripts, plugins) should be no-op'd here
        # rather than ACK'ing a PR / comment event.
        _logger.info(
            "triage_skipped_wrong_kind",
            extra={"delivery_id": event.delivery_id, "kind": event.kind.value},
        )
        return
    if event.is_from_bot:
        _logger.info(
            "triage_skipped_bot_actor",
            extra={"delivery_id": event.delivery_id, "actor": event.actor},
        )
        return

    if event.installation_id is None or event.issue_number is None:
        _logger.warning(
            "triage_skipped_missing_context",
            extra={
                "delivery_id": event.delivery_id,
                "kind": event.kind.value,
                "has_installation": event.installation_id is not None,
                "has_issue_number": event.issue_number is not None,
            },
        )
        return

    message = _ACK_TEMPLATE.format(actor=event.actor or "there")
    try:
        async with audit_lifecycle(ctx, workflow=Workflow.TRIAGE) as audit:
            result = await ctx.adapter.reply(event, message)
            audit.outcome = f"comment_id={result.get('id')}"
            _logger.info(
                "triage_ack_posted",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    "issue_number": event.issue_number,
                    "comment_id": result.get("id"),
                },
            )
    except Exception:
        _logger.exception(
            "triage_ack_failed",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "issue_number": event.issue_number,
            },
        )
