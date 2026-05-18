"""Issue → PR fix workflow — PRD §4.3 entry stub.

v0.1 Week 2 (slice A): ACK reply + audit. The agent loop (Modal clone
→ DeepAgent → push branch → open PR → self-fix on CI failure) lands
in the agent slice.

Slice A guarantees the same as `review.py` — pre-flight already ran;
this stub only acknowledges + audits, never raises.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openbot.application.workflows._lifecycle import audit_lifecycle
from openbot.infrastructure.persistence.models import Workflow

if TYPE_CHECKING:
    from openbot.application.middleware.preflight import PreflightContext

_logger = logging.getLogger(__name__)

_ACK_TEMPLATE = (
    ":robot: OpenBot received the fix assignment on issue #{issue} and the "
    "agent will start shortly.\n\n"
    "_v0.1 alpha: only the ACK is automated so far. Sandbox clone, "
    "DeepAgent loop, and PR creation land in an upcoming commit._"
)


async def maybe_run_fix(ctx: PreflightContext) -> None:
    event = ctx.event
    if event.issue_number is None or event.installation_id is None:
        _logger.info(
            "fix_skipped_missing_context",
            extra={"delivery_id": event.delivery_id, "kind": event.kind.value},
        )
        return

    message = _ACK_TEMPLATE.format(issue=event.issue_number)
    try:
        async with audit_lifecycle(ctx, workflow=Workflow.FIX) as audit:
            result = await ctx.adapter.reply(event, message)
            audit.outcome = f"comment_id={result.get('id')}"
            _logger.info(
                "fix_ack_posted",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    "issue_number": event.issue_number,
                    "comment_id": result.get("id"),
                },
            )
    except Exception:
        _logger.exception(
            "fix_ack_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
