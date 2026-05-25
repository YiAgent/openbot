"""PR review workflow — PRD §4.2 entry.

Slice B (current): structured-output reviewer + PR Review API submission.

  - Adapter fetches the unified diff (``get_pr_diff``).
  - ``DeepAgentsReviewResponder`` returns a ``ReviewFindings`` value.
  - Findings are filtered by ``ctx.config.severity_threshold``.
  - Inline findings (with ``line``) become PR review comments; repo-wide
    findings (no ``line``) are folded into the review body since the PR
    Review API requires a line number for inline comments.
  - Verdict is ``APPROVE`` when no findings survive the threshold,
    ``COMMENT`` otherwise. We intentionally never emit ``REQUEST_CHANGES``
    (PRD §13 — OpenBot is advisory in v0.1).
  - On any agent or submission failure, post a fallback ``COMMENT`` review
    with the error template so the PR author sees a real signal.

Slice C (deferred): the fix workflow uses the responder output to grade
its own attempts.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from openbot.application.ports.channel_adapter import ChannelAdapterPort
from openbot.application.use_cases._lifecycle import audit_lifecycle
from openbot.application.use_cases._tracing import observe as _observe
from openbot.application.use_cases._tracing import traceable as _traceable
from openbot.domain.events import UnifiedEvent
from openbot.domain.review import Finding, ReviewFindings, passes_threshold
from openbot.domain.workflows import Workflow
from openbot.infrastructure.agents import DeepAgentsReviewResponder

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from openbot.application.middleware.preflight import PreflightContext
    from openbot.domain.config_schema import SeverityThreshold

_logger = logging.getLogger(__name__)

_RESPONDER = DeepAgentsReviewResponder()

# Posted as a COMMENT review when the responder fails — the PR author
# sees a real signal instead of silence. Wraps a ``ReviewFindings``-like
# shape so the same submission path handles success and failure.
_ERROR_TEMPLATE = (
    ":robot: I couldn't complete the review right now.\n\n"
    "_Please retry by pushing a new commit, or check the worker logs / "
    "LLM credentials if this keeps failing._"
)


async def _generate_review_findings(
    *,
    event: UnifiedEvent,
    adapter: ChannelAdapterPort,
    run_id: str | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    per_task_cap_usd: Decimal,
    session_factory: Any,
) -> ReviewFindings:
    """Module-level seam — E2E tests monkeypatch this to avoid LLM calls."""
    return await _RESPONDER.review_for_event(
        event,
        adapter=adapter,
        run_id=run_id,
        checkpointer=checkpointer,
        per_task_cap_usd=per_task_cap_usd,
        session_factory=session_factory,
    )


def _filter_findings(
    findings: tuple[Finding, ...], threshold: SeverityThreshold
) -> tuple[Finding, ...]:
    """Keep findings at or above ``threshold``. Stable order preserved."""
    return tuple(f for f in findings if passes_threshold(f, threshold))


def _partition_findings(
    findings: tuple[Finding, ...],
) -> tuple[list[Finding], list[Finding]]:
    """Split into (inline, repo-wide).

    Inline findings have a ``line`` and become PR review comments.
    Repo-wide findings (``line is None``) get folded into the review body
    because the PR Review API rejects inline comments without a line.
    """
    inline: list[Finding] = []
    repo_wide: list[Finding] = []
    for f in findings:
        (inline if f.line is not None else repo_wide).append(f)
    return inline, repo_wide


def _format_finding_body(finding: Finding) -> str:
    """Render one finding as the comment body for a PR review comment."""
    head = f"**{finding.severity}** — {finding.message}"
    if finding.quote:
        return f"{head}\n\n```\n{finding.quote}\n```"
    return head


def _format_repo_wide_section(findings: list[Finding]) -> str:
    """Format repo-wide findings as a bullet list for the review body."""
    lines = ["", "**Repo-wide notes:**"]
    for f in findings:
        lines.append(f"- _{f.severity}_ — `{f.file}`: {f.message}")
    return "\n".join(lines)


def _build_review_body(summary: str, repo_wide: list[Finding]) -> str:
    """The text that renders at the top of the PR review."""
    if not repo_wide:
        return summary
    return summary + "\n" + _format_repo_wide_section(repo_wide)


def _build_inline_comments(inline: list[Finding]) -> list[dict[str, object]]:
    """Shape inline findings for the PR Review API."""
    return [
        {
            "path": f.file,
            "line": f.line,
            "body": _format_finding_body(f),
        }
        for f in inline
        if f.line is not None
    ]


@_observe(name="review", capture_input=False)
@_traceable(run_type="chain", name="review")
async def maybe_run_review(ctx: PreflightContext) -> None:
    """Run the DeepAgent review + submit findings via the PR Review API.

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

    run_id = ctx.dispatch.run_id
    checkpointer = ctx.agent_checkpointer

    try:
        # Generate the review BEFORE opening the audit-lifecycle span so a
        # slow LLM call doesn't keep a STARTED row sitting open for minutes.
        findings: ReviewFindings | None = None
        try:
            findings = await _generate_review_findings(
                event=event,
                adapter=ctx.adapter,
                run_id=run_id,
                checkpointer=checkpointer,
                per_task_cap_usd=ctx.config.budget.per_task_cap_usd,
                session_factory=ctx.session_factory,
            )
        except Exception:
            _logger.exception(
                "review_agent_reply_failed",
                extra={"delivery_id": event.delivery_id, "repo": event.repo},
            )

        # Build the PR Review submission. On responder failure: an error-body
        # COMMENT review with no inline comments. On success: filter →
        # partition → format.
        if findings is None:
            body = _ERROR_TEMPLATE
            event_type: Literal["APPROVE", "COMMENT"] = "COMMENT"
            comments: list[dict[str, object]] = []
            kept = 0
        else:
            filtered = _filter_findings(findings.findings, ctx.config.severity_threshold)
            inline, repo_wide = _partition_findings(filtered)
            body = _build_review_body(findings.summary, repo_wide)
            comments = _build_inline_comments(inline)
            # Per PRD §13 lock: APPROVE on zero filtered findings, never REQUEST_CHANGES.
            event_type = "APPROVE" if not filtered else "COMMENT"
            kept = len(filtered)

        try:
            async with audit_lifecycle(ctx, workflow=Workflow.REVIEW) as audit:
                result = await ctx.adapter.create_pr_review(
                    event,
                    event.pr_number,
                    body=body,
                    event_type=event_type,
                    comments=comments or None,
                )
                audit.outcome = f"review_id={result.get('id')} event={event_type} findings={kept}"
                _logger.info(
                    "review_posted",
                    extra={
                        "delivery_id": event.delivery_id,
                        "repo": event.repo,
                        "pr_number": event.pr_number,
                        "review_id": result.get("id"),
                        "event_type": event_type,
                        "findings_kept": kept,
                    },
                )
        except Exception:
            # audit_lifecycle already wrote FAILED. Swallow so the background
            # task doesn't surface as 5xx — GitHub already got its 202.
            _logger.exception(
                "review_post_failed",
                extra={"delivery_id": event.delivery_id, "repo": event.repo},
            )
    finally:
        # Cleanup: delete LangGraph checkpoint rows regardless of outcome
        # (success, agent failure, post failure, CancelledError, etc.) so
        # they don't accumulate in Postgres indefinitely.
        if run_id and checkpointer is not None:
            try:
                await checkpointer.adelete_thread(run_id)
            except Exception:
                _logger.warning(
                    "review_checkpoint_delete_failed",
                    extra={"run_id": run_id, "delivery_id": event.delivery_id, "repo": event.repo},
                )
