"""@openbot chat workflow — PRD §4.4 entry stub.

Slice C wires `chat_parser.parse` so a `@openbot help` returns a canned
help reply (vs. the generic ACK) and a `@openbot <freeform>` still
reaches the LLM path (currently still a stub — the tool-using chat
agent lands once Modal + LangGraph are integrated).

Cancel parsing lives in `CancelCommentMiddleware`; by the time this
handler runs, any cancel comment has already returned BLOCKED upstream.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openbot.application.workflows._lifecycle import audit_lifecycle
from openbot.application.workflows.chat_parser import parse as parse_chat_command
from openbot.infrastructure.persistence.models import Workflow

if TYPE_CHECKING:
    from openbot.application.middleware.preflight import PreflightContext

_logger = logging.getLogger(__name__)

_ACK_TEMPLATE = (
    ":robot: Hi @{actor} — OpenBot received your message and is thinking.\n\n"
    "_v0.1 alpha: only the ACK is automated so far. Tool-using chat agent "
    "(read_file / grep / web_fetch / search) lands in an upcoming commit._"
)

_HELP_TEMPLATE = (
    ":robot: **OpenBot chat** — v0.1 alpha\n\n"
    "Usage: `@openbot <your question or request>`\n\n"
    "**Available structural commands**\n"
    "- `@openbot stop` / `cancel` / `停` / `取消` — cancel the current task\n"
    "- `@openbot help` / `?` / `帮助` — show this message\n\n"
    "Anything else is treated as a freeform request. v0.1 alpha replies with "
    "an ACK only; tool-using responses arrive in an upcoming commit.\n\n"
    "Docs: https://github.com/YiAgent/openbot/blob/main/docs/prd/openbot-prd.md"
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

    command = parse_chat_command(event.comment_body)
    if command is None:
        # Router accepted this as a chat mention but our stricter parser
        # rejected it (e.g. lookalike login, body too long). Drop quietly
        # — replying would just spam.
        _logger.info(
            "chat_skipped_unparseable_mention",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
        return

    # Branch on structural intent:
    #   is_cancel   → never reached in practice (CancelCommentMiddleware
    #                 short-circuits earlier); defensive no-op.
    #   is_help     → canned help reply (cheap, no LLM call).
    #   freeform    → LLM ACK stub.
    if command.is_cancel:
        _logger.info(
            "chat_cancel_reached_workflow_unexpected",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
        return

    message = (
        _HELP_TEMPLATE if command.is_help else _ACK_TEMPLATE.format(actor=event.actor or "there")
    )

    try:
        async with audit_lifecycle(ctx, workflow=Workflow.CHAT) as audit:
            result = await ctx.adapter.reply(event, message)
            outcome_intent = "help" if command.is_help else "freeform"
            audit.outcome = f"intent={outcome_intent} comment_id={result.get('id')}"
            _logger.info(
                "chat_ack_posted",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    "target": target_number,
                    "intent": outcome_intent,
                    "comment_id": result.get("id"),
                },
            )
    except Exception:
        _logger.exception(
            "chat_ack_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
