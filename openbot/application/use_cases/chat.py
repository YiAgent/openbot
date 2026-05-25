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
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from openbot.application.state.cancellation import checkpoint
from openbot.application.use_cases._lifecycle import audit_lifecycle, sticky_reply
from openbot.application.use_cases._tracing import observe as _observe
from openbot.application.use_cases._tracing import traceable as _traceable
from openbot.application.use_cases.chat_parser import parse as parse_chat_command
from openbot.domain.workflows import Workflow
from openbot.infrastructure.agents import DeepAgentsChatResponder

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from openbot.application.middleware.preflight import PreflightContext

_logger = logging.getLogger(__name__)
_RESPONDER = DeepAgentsChatResponder()

_ACK_TEMPLATE = (
    ":robot: Hi @{actor} — OpenBot received your message and is thinking.\n\n"
    "_v0.1 alpha: only the ACK is automated so far. Tool-using chat agent "
    "(read_file / grep / web_fetch / search) lands in an upcoming commit._"
)
_THINKING_TEMPLATE = (
    ":robot: Hi @{actor} — OpenBot received your message and is thinking…\n\n"
    "_This comment will be updated with the response._"
)
_ERROR_TEMPLATE = (
    ":robot: I couldn't complete that request right now.\n\n"
    "Please try again shortly. If this keeps failing, check the worker logs and LLM credentials."
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


async def _generate_freeform_reply(
    *,
    event,
    user_request: str,
    run_id: str | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    adapter=None,
    per_task_cap_usd: Decimal,
    session_factory: Any,
) -> str:
    return await _RESPONDER.reply_for_event(
        event,
        user_request=user_request,
        run_id=run_id,
        checkpointer=checkpointer,
        adapter=adapter,
        per_task_cap_usd=per_task_cap_usd,
        session_factory=session_factory,
    )


@_observe(name="chat", capture_input=False)
@_traceable(run_type="chain", name="chat")
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

    run_id = ctx.dispatch.run_id
    checkpointer = ctx.agent_checkpointer

    # Branch on structural intent:
    #   is_cancel   → never reached in practice (CancelCommentMiddleware
    #                 short-circuits earlier); defensive no-op.
    #   is_help     → canned reply (instant, no LLM call).
    #   no body     → ACK only (instant).
    #   freeform    → sticky: post placeholder first, update after LLM.
    if command.is_cancel:
        _logger.info(
            "chat_cancel_reached_workflow_unexpected",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
        return

    adapter = ctx.adapter

    if command.is_freeform and command.body_after_mention:
        # Freeform LLM request — use sticky comment so the LLM response
        # replaces the placeholder in-place rather than posting a new thread.
        initial = _THINKING_TEMPLATE.format(actor=event.actor or "there")
        try:
            async with sticky_reply(adapter, event, initial=initial) as sticky:
                try:
                    message = await _generate_freeform_reply(
                        event=event,
                        user_request=command.body_after_mention,
                        run_id=run_id,
                        checkpointer=checkpointer,
                        adapter=ctx.adapter,
                        per_task_cap_usd=ctx.config.budget.per_task_cap_usd,
                        session_factory=ctx.session_factory,
                    )
                except Exception:
                    _logger.exception(
                        "chat_agent_reply_failed",
                        extra={"delivery_id": event.delivery_id, "repo": event.repo},
                    )
                    message = _ERROR_TEMPLATE

                if run_id:
                    await checkpoint(ctx.redis, run_id)

                try:
                    async with audit_lifecycle(ctx, workflow=Workflow.CHAT) as audit:
                        await sticky.update(message)
                        audit.outcome = f"intent=freeform comment_id={sticky.comment_id}"
                        _logger.info(
                            "chat_freeform_posted",
                            extra={
                                "delivery_id": event.delivery_id,
                                "repo": event.repo,
                                "target": target_number,
                                "comment_id": sticky.comment_id,
                            },
                        )
                        if run_id and checkpointer is not None:
                            try:
                                await checkpointer.adelete_thread(run_id)
                            except Exception:
                                _logger.warning(
                                    "chat_checkpoint_delete_failed",
                                    extra={"delivery_id": event.delivery_id, "run_id": run_id},
                                )
                except Exception:
                    _logger.exception(
                        "chat_freeform_post_failed",
                        extra={"delivery_id": event.delivery_id, "repo": event.repo},
                    )
            message = await _generate_freeform_reply(
                event=event,
                user_request=command.body_after_mention,
                run_id=run_id,
                checkpointer=checkpointer,
            )
        except Exception:
            _logger.exception(
                "chat_freeform_failed",
                extra={"delivery_id": event.delivery_id, "repo": event.repo},
            )
        return

    # Instant paths (help / bare @mention): message is known immediately,
    # no sticky needed — single POST is cleaner than placeholder + PATCH.
    if command.is_help:
        message = _HELP_TEMPLATE
    else:
        message = _ACK_TEMPLATE.format(actor=event.actor or "there")

    # Cancellation checkpoint before the audit row.
    if run_id:
        await checkpoint(ctx.redis, run_id)

    try:
        async with audit_lifecycle(ctx, workflow=Workflow.CHAT) as audit:
            result = await adapter.reply(event, message)
            outcome_intent = "help" if command.is_help else "ack"
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
            if run_id and checkpointer is not None:
                try:
                    await checkpointer.adelete_thread(run_id)
                except Exception:
                    _logger.warning(
                        "chat_checkpoint_delete_failed",
                        extra={"delivery_id": event.delivery_id, "run_id": run_id},
                    )
    except Exception:
        _logger.exception(
            "chat_ack_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
