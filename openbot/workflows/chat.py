"""@openbot chat workflow — PRD §4.4 entry stub.

v0.1 Week 2 (slice A): ACK reply + audit. The tool-using chat agent
(read_file / glob / grep / shell_readonly / web_fetch / search_*)
lands once the agent slice ships.

Cancel comment parsing (`@openbot stop` / 停 / 取消) lives in slice C's
cancel middleware, not here — by the time the dispatch reaches this
stub the cancel middleware has already returned BLOCKED for those.
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
    ":robot: Hi @{actor} — OpenBot received your message and is thinking.\n\n"
    "_v0.1 alpha: only the ACK is automated so far. Tool-using chat agent "
    "(read_file / grep / web_fetch / search) lands in an upcoming commit._"
)


async def maybe_run_chat(ctx: PreflightContext) -> None:
    event = ctx.event
    # Chat replies to whichever surface carried the @mention — issue or PR.
    target_number = event.issue_number or event.pr_number
    if target_number is None or event.installation_id is None:
        _logger.info(
            "chat_skipped_missing_context",
            extra={"delivery_id": event.delivery_id, "kind": event.kind.value},
        )
        return

    message = _ACK_TEMPLATE.format(actor=event.actor or "there")
    try:
        async with audit_lifecycle(ctx, workflow=Workflow.CHAT) as audit:
            result = await ctx.adapter.reply(event, message)
            audit.outcome = f"comment_id={result.get('id')}"
            _logger.info(
                "chat_ack_posted",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    "target": target_number,
                    "comment_id": result.get("id"),
                },
            )
    except Exception:
        _logger.exception(
            "chat_ack_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
