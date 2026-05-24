"""Triage workflow — PRD §4.1.

Handles issues.opened, issues.edited, and issues.reopened.

Pipeline (current state):
  1. Guard: skip if kind is not a triage-triggering event, bot-authored, or
     missing installation_id / issue_number.
  2. ACK: post a comment so the reporter knows OpenBot saw the issue.
  3. Label: if the LLM classifier ran, apply a type label (bug / enhancement /
     question / spam) and a priority label (priority:critical / priority:high /
     priority:medium / priority:low). Label failures are best-effort — the ACK
     comment already landed.

PRD §4.1's sandbox reproduce step is conditional on the classifier reporting
has_reproduction_info=True for a bug; that logic lands in an upcoming commit.
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
    from openbot.dispatcher.classifier import TriageClassifierOutput

_logger = logging.getLogger("openbot.application.use_cases.triage")

# Event kinds that should trigger the triage pipeline. Router dispatches
# OPENED, EDITED, REOPENED to TRIAGE; LABELED/UNLABELED pass through the
# state machine (for cancel-openbot detection) but do not need a user reply.
_TRIAGE_KINDS = frozenset(
    {EventKind.ISSUE_OPENED, EventKind.ISSUE_EDITED, EventKind.ISSUE_REOPENED}
)

# Map classifier type → GitHub label. These are GitHub's built-in defaults
# present on every new repository, so add_label rarely 404s.
_TYPE_TO_LABEL: dict[str, str] = {
    "bug": "bug",
    "feature": "enhancement",
    "question": "question",
    "spam": "spam",
    # "other" intentionally omitted — no useful default label exists.
}

_PRIORITY_TO_LABEL: dict[str, str] = {
    "critical": "priority:critical",
    "high": "priority:high",
    "medium": "priority:medium",
    "low": "priority:low",
}

_ACK_TEMPLATE = (
    ":robot: Hi @{actor} — OpenBot received this issue and will triage shortly.\n\n"
    "_v0.1 alpha: ACK + auto-label are live. "
    "Sandbox reproduce evidence lands in an upcoming commit._"
)

_ACK_WITH_LABEL_TEMPLATE = (
    ":robot: Hi @{actor} — OpenBot triaged this issue.\n\n"
    "**Classification:** {type_label} · **Priority:** {priority}\n\n"
    "_Labels applied automatically. If the classification looks wrong, "
    "feel free to update the labels manually._"
)


async def _apply_triage_labels(ctx: PreflightContext, output: TriageClassifierOutput) -> None:
    """Best-effort: apply type + priority labels from classifier output.

    Swallows all errors — label failures must not block the ACK comment
    or the audit row. The triage feature owns its own log line so ops can
    track the failure rate without alerting.
    """
    labels_to_add: list[str] = []

    type_label = _TYPE_TO_LABEL.get(output.type)
    if type_label:
        labels_to_add.append(type_label)

    priority_label = _PRIORITY_TO_LABEL.get(output.severity_guess)
    if priority_label:
        labels_to_add.append(priority_label)

    if not labels_to_add:
        return

    try:
        await ctx.adapter.add_label(ctx.event, *labels_to_add)
        _logger.info(
            "triage_labels_applied",
            extra={
                "delivery_id": ctx.event.delivery_id,
                "repo": ctx.event.repo,
                "issue_number": ctx.event.issue_number,
                "labels": labels_to_add,
            },
        )
    except Exception:
        _logger.warning(
            "triage_label_apply_failed",
            extra={
                "delivery_id": ctx.event.delivery_id,
                "repo": ctx.event.repo,
                "issue_number": ctx.event.issue_number,
                "labels": labels_to_add,
            },
            exc_info=True,
        )


@_observe(name="triage", capture_input=False)
@_traceable(run_type="chain", name="triage")
async def maybe_run_triage(ctx: PreflightContext) -> None:
    event = ctx.event
    if event.kind not in _TRIAGE_KINDS:
        # ISSUE_LABELED / ISSUE_UNLABELED reach here via the NO_SANDBOX router
        # path so the state machine can handle cancel-openbot signals. They do
        # not need a user-visible reply.
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

    # Read classifier output (populated by the worker before calling this handler).
    # Import here to avoid a circular import via the dispatcher module.
    from openbot.dispatcher.classifier import TriageClassifierOutput

    classifier: TriageClassifierOutput | None = (
        ctx.classifier_output
        if isinstance(ctx.classifier_output, TriageClassifierOutput)
        else None
    )

    if classifier is not None:
        type_label = _TYPE_TO_LABEL.get(classifier.type, "")
        priority_label = _PRIORITY_TO_LABEL.get(classifier.severity_guess, "")
        message = _ACK_WITH_LABEL_TEMPLATE.format(
            actor=event.actor or "there",
            type_label=type_label or classifier.type,
            priority=priority_label.replace("priority:", "") if priority_label else classifier.severity_guess,
        )
    else:
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
                    "kind": event.kind.value,
                },
            )
            # Apply labels after the ACK lands. Failures are best-effort.
            if classifier is not None:
                await _apply_triage_labels(ctx, classifier)
    except Exception:
        _logger.exception(
            "triage_ack_failed",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "issue_number": event.issue_number,
            },
        )
