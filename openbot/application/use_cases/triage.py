"""Triage workflow — PRD §4.1 + reproduce-agent slice R4.

Slice R4 wires the reproduce decision into the triage handler:

  - For every triage-triggered event, post a sticky placeholder
    comment (``render_thinking_comment``) so the reporter sees
    immediate acknowledgement.
  - Decide whether to run the reproduce agent via
    ``_should_run_reproduce(ctx)``. The predicate is the *intersection*
    of two signals:

      1. ``derive_sandbox_policy(...)`` returns ``REQUIRED`` — the
         classifier saw a bug-with-repro signal (or fell back to the
         static REQUIRED default).
      2. ``ctx.sandbox_handle is not None`` — the dispatcher actually
         provisioned a sandbox (provisioning could have failed even
         when the policy said REQUIRED).

    Either gate failing → patch the placeholder with the ACK template
    and exit. Both gates passing → call ``_generate_repro_outcome`` and
    PATCH the placeholder with the rendered status template.

  - On responder errors translate to an ``AGENT_FAILED`` outcome —
    the comment body is the spec §5.3 verbatim string (NO exception
    class name leaks to GitHub); the audit row carries
    ``reproduce:agent_failed:<ExceptionClassName>`` for ops.

  - ``RunCancelledError`` propagates through ``audit_lifecycle`` which
    writes a CANCELLED row before re-raising; we catch the re-raise at
    the workflow boundary so the background task returns cleanly.

  - ``sticky_reply`` is invoked with ``fallback_on_update_error=True``
    so a PATCH failure falls back to a fresh ``reply()`` — losing the
    outcome message would be worse than a duplicate comment.

Behavior preserved from earlier slices:

  - Skip bot-authored issues (PRD §4 echo-loop defense; Router also
    blocks these but defense-in-depth is cheap).
  - Skip when ``installation_id`` or ``issue_number`` is missing.
  - Auto-label type + priority from classifier output (best-effort).
  - Never raise — background-task failures must not surface as 5xx.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openbot.application.router import SandboxPolicy
from openbot.application.sandbox_policy import derive_sandbox_policy
from openbot.application.use_cases._lifecycle import audit_lifecycle, sticky_reply
from openbot.application.use_cases._repro_render import (
    render_final_comment,
    render_thinking_comment,
)
from openbot.application.use_cases._tracing import observe as _observe
from openbot.application.use_cases._tracing import traceable as _traceable
from openbot.domain.events import EventKind
from openbot.domain.repro import ReproOutcome, ReproStatus
from openbot.domain.workflows import Feature, Workflow
from openbot.infrastructure.agents import DeepAgentsReproResponder

if TYPE_CHECKING:
    from openbot.application.middleware.preflight import PreflightContext
    from openbot.dispatcher.classifier import TriageClassifierOutput

_logger = logging.getLogger("openbot.application.use_cases.triage")

_TRIAGE_KINDS = frozenset(
    {EventKind.ISSUE_OPENED, EventKind.ISSUE_EDITED, EventKind.ISSUE_REOPENED}
)

_TYPE_TO_LABEL: dict[str, str] = {
    "bug": "bug",
    "feature": "enhancement",
    "question": "question",
    "spam": "spam",
}

_PRIORITY_TO_LABEL: dict[str, str] = {
    "critical": "priority:critical",
    "high": "priority:high",
    "medium": "priority:medium",
    "low": "priority:low",
}

# Kept exposed at module level for the renderer's ``ack_only`` branch.
_ACK_TEMPLATE = (
    ":robot: Hi @{actor} — OpenBot received this issue and will triage shortly.\n\n"
    "_v0.1 alpha: automated labeling and priority coming in upcoming commits._"
)

# One responder per process: thread-safe; in reproduce-agent slice the
# per-event sandbox handle is passed through ``reproduce_for_event``.
_RESPONDER = DeepAgentsReproResponder()


# ── Predicate ───────────────────────────────────────────────────────────────


def _should_run_reproduce(ctx: PreflightContext) -> bool:
    """Decide whether the reproduce agent should run for this event.

    The predicate is the AND of:

      1. ``derive_sandbox_policy`` returns ``REQUIRED`` for this
         classifier output, with the triage static default.
      2. ``ctx.sandbox_handle is not None`` — provisioning actually
         succeeded.

    Returns ``False`` if either gate fails; the caller then renders the
    ACK template instead of attempting reproduce.
    """
    policy = derive_sandbox_policy(
        static=SandboxPolicy.REQUIRED,
        classifier_output=ctx.classifier_output,
        feature=Feature.TRIAGE,
    )
    if policy is not SandboxPolicy.REQUIRED:
        return False
    return ctx.sandbox_handle is not None


# ── Label helpers ───────────────────────────────────────────────────────────


async def _apply_triage_labels(ctx: PreflightContext, output: TriageClassifierOutput) -> None:
    """Best-effort: apply type + priority labels from classifier output."""
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


# ── Responder seam ──────────────────────────────────────────────────────────


async def _generate_repro_outcome(ctx: PreflightContext) -> ReproOutcome:
    """Module-level seam — E2E tests monkeypatch this to skip DeepAgents.

    Fetches the issue payload via the adapter, then delegates to the
    responder.  The sticky-reply / audit / rendering plumbing stays in
    ``maybe_run_triage`` so this function has exactly one job: produce
    a ``ReproOutcome``.
    """
    event = ctx.event
    handle = ctx.sandbox_handle
    assert handle is not None, "_generate_repro_outcome called without sandbox_handle"
    assert event.issue_number is not None, "_generate_repro_outcome called without issue_number"

    issue = await ctx.adapter.get_issue(event, event.issue_number)
    return await _RESPONDER.reproduce_for_event(
        event,
        adapter=ctx.adapter,
        sandbox=handle.sandbox,
        issue=issue,
        run_id=ctx.dispatch.run_id,
        checkpointer=ctx.agent_checkpointer,
    )


# ── AGENT_FAILED fallback outcome ───────────────────────────────────────────


def _agent_failed_outcome() -> ReproOutcome:
    return ReproOutcome(
        status=ReproStatus.AGENT_FAILED,
        summary="",
        command=None,
        exit_code=None,
        output_excerpt="",
        hypothesis="",
        repro_artifact=None,
        repro_artifact_filename=None,
    )


# ── Entrypoint ──────────────────────────────────────────────────────────────


@_observe(name="triage", capture_input=False)
@_traceable(run_type="chain", name="triage")
async def maybe_run_triage(ctx: PreflightContext) -> None:
    """Run the triage workflow for one event.

    See module docstring for the full decision flow. ``ctx`` is the
    immutable preflight snapshot; nothing here mutates it.
    """
    event = ctx.event
    if event.kind not in _TRIAGE_KINDS:
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

    # Resolve classifier output (populated by the worker before calling).
    from openbot.dispatcher.classifier import TriageClassifierOutput

    classifier: TriageClassifierOutput | None = (
        ctx.classifier_output if isinstance(ctx.classifier_output, TriageClassifierOutput) else None
    )

    adapter = ctx.adapter
    run_id = ctx.dispatch.run_id
    checkpointer = ctx.agent_checkpointer

    try:
        async with (
            audit_lifecycle(ctx, workflow=Workflow.TRIAGE) as audit,
            sticky_reply(
                adapter,
                event,
                initial=render_thinking_comment(actor=event.actor),
                fallback_on_update_error=True,
            ) as sticky,
        ):
            if not _should_run_reproduce(ctx):
                await sticky.update(render_final_comment(None, actor=event.actor, ack_only=True))
                audit.outcome = "ack_only"
                # Best-effort label application after the ACK lands.
                if classifier is not None:
                    await _apply_triage_labels(ctx, classifier)
                return

            outcome: ReproOutcome
            try:
                outcome = await _generate_repro_outcome(ctx)
            except Exception as exc:
                outcome = _agent_failed_outcome()
                audit.outcome = f"reproduce:agent_failed:{type(exc).__name__}"
                _logger.exception(
                    "triage_reproduce_agent_failed",
                    extra={
                        "delivery_id": event.delivery_id,
                        "repo": event.repo,
                        "issue_number": event.issue_number,
                    },
                )
            else:
                suffix = ":no_comment" if sticky.comment_id is None else ""
                audit.outcome = f"reproduce:{outcome.status.value}{suffix}"

            # Apply labels after handling outcome (best-effort).
            if classifier is not None:
                await _apply_triage_labels(ctx, classifier)

            await sticky.update(render_final_comment(outcome, actor=event.actor))
    except Exception:
        _logger.exception(
            "triage_failed",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "issue_number": event.issue_number,
            },
        )
    finally:
        if run_id and checkpointer is not None:
            try:
                await checkpointer.adelete_thread(run_id)
            except Exception:
                _logger.warning(
                    "triage_checkpoint_delete_failed",
                    extra={
                        "run_id": run_id,
                        "delivery_id": event.delivery_id,
                        "repo": event.repo,
                    },
                )


__all__ = ["maybe_run_triage"]
