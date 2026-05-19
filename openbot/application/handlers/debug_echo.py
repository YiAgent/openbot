"""Debug echo handler — structured trace, three sinks.

When ``OPENBOT_DEBUG_ECHO=1`` the Router resolves every classified
feature (TRIAGE / REVIEW / FIX / CHAT) to this handler instead of the
real workflow stub. The point is to verify the receive→queue→worker
→middleware→handler pipeline end-to-end on the live Heroku worker with
zero LLM cost, before agent logic lands.

For each delivery the handler emits the same trace through three
parallel sinks:

  1. **GitHub comment** — via ``adapter.reply(event, msg)`` when the
     adapter was constructed with an auth handle (i.e. App credentials
     are present). When auth is absent the comment is silently
     skipped — receive-only deployments still get the other two sinks.
  2. **Structured JSON log** — always-on, machine-greppable, visible
     in ``heroku logs --dyno worker``. Single ``debug_echo`` log key so
     dashboards can filter on it cleanly.
  3. **Audit row** (PRD §9.4) — written via the existing
     ``AuditLogRepo`` if a session factory is wired, otherwise skipped.
     ``details`` JSON carries the same trace fields as the log line.

Cancellation: the handler hits ``cancellation.checkpoint(run_id)`` twice
— once at entry, once before posting the GitHub comment. The 250 ms
``asyncio.sleep`` between checkpoints exists so a SUPERSEDE arriving
mid-handler can actually land its cancel signal before the first
checkpoint clears.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from openbot.application.state.cancellation import RunCancelledError, checkpoint
from openbot.infrastructure.persistence.models import Workflow, WorkflowPhase
from openbot.infrastructure.persistence.repository import AuditLogRepo

if TYPE_CHECKING:
    from openbot.application.middleware.preflight import PreflightContext

_logger = logging.getLogger(__name__)

# Sleep between checkpoints. Long enough that a SUPERSEDE arriving on
# the queue right after this handler started can land its cancel
# signal before the second checkpoint fires; short enough that an
# operator dogfooding the live worker doesn't have to wait long for
# the echo to land. 250 ms matches the figure in the plan.
_WORK_SIMULATION_SECONDS = 0.25

# Feature → Workflow enum mapping for the audit row. Kept here (not in
# the audit repo) so the audit_log column reuses the existing
# WorkflowPhase / Workflow enums rather than inventing a parallel
# debug-only set.
_FEATURE_TO_WORKFLOW = {
    "triage": Workflow.TRIAGE,
    "review": Workflow.REVIEW,
    "fix": Workflow.FIX,
    "chat": Workflow.CHAT,
}


def _format_trace(ctx: PreflightContext) -> str:
    """Compose the human-readable echo message.

    Stable, deterministic output so an operator can copy/paste the
    posted comment into a bug report and reviewers can match it
    against the corresponding ``debug_echo`` log line.
    """
    event = ctx.event
    dispatch = ctx.dispatch
    worker = os.environ.get("DYNO", "unknown")
    pid = os.getpid()
    return (
        "🤖 OpenBot debug-echo\n"
        f"• event:        {event.kind.value}\n"
        f"• resource:     {dispatch.resource_key or event.resource_key or '(none)'}\n"
        f"• intent:       {(dispatch.intent or 'start').upper()}\n"
        f"• prev_run_id:  {dispatch.prev_run_id or '—'}\n"
        f"• run_id:       {dispatch.run_id or dispatch.task_id}\n"
        f"• event_seq:    {dispatch.event_seq}\n"
        f"• feature:      {dispatch.feature.value}\n"
        f"• worker:       {worker} pid={pid}\n"
        f"• ts:           {datetime.now(UTC).isoformat()}\n"
    )


def _trace_extras(ctx: PreflightContext) -> dict[str, object]:
    """Shared field set for the JSON log + audit_log.details JSON."""
    event = ctx.event
    dispatch = ctx.dispatch
    return {
        "channel": event.channel,
        "delivery_id": event.delivery_id,
        "kind": event.kind.value,
        "repo": event.repo,
        "actor": event.actor,
        "actor_type": event.actor_type,
        "feature": dispatch.feature.value,
        "intent": (dispatch.intent or "start").upper(),
        "resource_key": dispatch.resource_key or event.resource_key,
        "run_id": dispatch.run_id or dispatch.task_id,
        "prev_run_id": dispatch.prev_run_id,
        "event_seq": dispatch.event_seq,
        "worker": os.environ.get("DYNO", "unknown"),
        "pid": os.getpid(),
    }


async def debug_echo_handler(ctx: PreflightContext) -> None:
    """Single async callable wired in via ``router._resolve_handler``.

    Never raises — errors in any sink are logged and the rest of the
    sinks still fire. The receive side has already 202'd and the
    worker has already XACK'd (or will, on success); we don't want a
    transient Postgres flap to escalate to a queue retry.
    """
    run_id = ctx.dispatch.run_id or ctx.dispatch.task_id
    extras = _trace_extras(ctx)

    # Pre-work cancellation check — a SUPERSEDE that arrived between
    # enqueue and the worker picking us up may already be flagged.
    try:
        await checkpoint(ctx.redis, run_id)
    except RunCancelledError:
        _logger.info("debug_echo_cancelled_pre", extra=extras)
        return

    # Simulate work — gives a SUPERSEDE arriving right after enqueue a
    # window to plant the cancel flag before our second checkpoint.
    await asyncio.sleep(_WORK_SIMULATION_SECONDS)

    try:
        await checkpoint(ctx.redis, run_id)
    except RunCancelledError:
        _logger.info("debug_echo_cancelled_mid", extra=extras)
        return

    # Sink 1: structured log. Cheap, always-on.
    _logger.info("debug_echo", extra=extras)

    # Sink 2: GitHub comment when we have write-back. The adapter
    # raises if no auth was configured; we degrade silently rather
    # than crash the handler — receive-only deployments are a
    # supported configuration.
    try:
        # Only reply when we have an issue/PR number to attach to.
        if (ctx.event.issue_number or ctx.event.pr_number) and ctx.event.installation_id:
            await ctx.adapter.reply(ctx.event, _format_trace(ctx))
    except Exception:
        # Auth missing, permission error, network blip — log + continue.
        _logger.warning("debug_echo_reply_failed", extra=extras, exc_info=True)

    # Sink 3: audit_log row. The middleware chain already wrote a
    # STARTED row via AuditStartMiddleware; we add a COMPLETED row
    # so the lifecycle is visible end-to-end.
    if ctx.session_factory is not None:
        workflow = _FEATURE_TO_WORKFLOW.get(ctx.dispatch.feature.value)
        try:
            async with ctx.session_factory() as session:
                audit_repo = AuditLogRepo(session)
                await audit_repo.write(
                    delivery_id=ctx.event.delivery_id,
                    repo=ctx.event.repo,
                    actor=ctx.event.actor,
                    workflow=workflow.value if workflow else None,
                    phase=WorkflowPhase.COMPLETED.value,
                    outcome="debug_echo",
                    # SQLAlchemy JSON columns can't hold non-serializable
                    # values; trace fields are all primitives so this is
                    # safe, but cast to dict[str, Any] for the type hint.
                    details=dict(extras),
                )
                await session.commit()
        except Exception:
            _logger.exception("debug_echo_audit_write_failed", extra=extras)


__all__ = ["debug_echo_handler"]
