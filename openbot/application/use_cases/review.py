"""PR review workflow — PRD §4.2 entry.

Slice A (current): single-shot DeepAgent review.
  - Adapter fetches the unified diff (``get_pr_diff``).
  - ``DeepAgentsReviewResponder`` runs once with the diff inline (no tools).
  - The agent's reply is posted as a single PR comment.
  - On any agent failure, post a user-facing error stub so the PR author
    sees a real signal — silent skips break trust.

Slice B/C (deferred — see docs/superpowers/plans/2026-05-20-review-fix-deepagent.md):
  structured findings, severity filter, multi-turn, file/grep tools.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openbot.application.ports.channel_adapter import ChannelAdapterPort
from openbot.application.use_cases._lifecycle import audit_lifecycle
from openbot.application.use_cases._tracing import traceable as _traceable
from openbot.domain.events import UnifiedEvent
from openbot.domain.workflows import Workflow
from openbot.infrastructure.agents import DeepAgentsReviewResponder

if TYPE_CHECKING:
    from openbot.application.middleware.preflight import PreflightContext

_logger = logging.getLogger(__name__)

_RESPONDER = DeepAgentsReviewResponder()

_ERROR_TEMPLATE = (
    ":robot: I couldn't complete the review right now.\n\n"
    "_Please retry by pushing a new commit, or check the worker logs / "
    "LLM credentials if this keeps failing._"
)


async def _generate_review_reply(*, event: UnifiedEvent, adapter: ChannelAdapterPort) -> str:
    """Module-level seam — E2E tests monkeypatch this to avoid LLM calls."""
    return await _RESPONDER.review_for_event(event, adapter=adapter)


@_traceable(run_type="chain", name="review")
async def maybe_run_review(ctx: PreflightContext) -> None:
    """Run the DeepAgent review + audit-log the run.

    No-op if ``pr_number`` or ``installation_id`` is missing — Router
    already enforces this for the dispatch path, the check here is
    defense-in-depth for callers that bypass dispatch.
    """
    event = ctx.event
    if event.pr_number is None or event.installation_id is None:
        _logger.info(
            "review_skipped_missing_context",
            extra={"delivery_id": event.delivery_id, "kind": event.kind.value},
        )
        return

    # Generate the review BEFORE opening the audit-lifecycle span so a
    # slow LLM call doesn't keep a STARTED row sitting open for minutes.
    # The reply post inside the lifecycle is the canonical "did we
    # actually act on the PR?" signal.
    try:
        message = await _generate_review_reply(event=event, adapter=ctx.adapter)
    except Exception:
        _logger.exception(
            "review_agent_reply_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
        message = _ERROR_TEMPLATE

    try:
        async with audit_lifecycle(ctx, workflow=Workflow.REVIEW) as audit:
            result = await ctx.adapter.reply(event, message)
            audit.outcome = f"comment_id={result.get('id')}"
            _logger.info(
                "review_reply_posted",
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
            "review_reply_post_failed",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
