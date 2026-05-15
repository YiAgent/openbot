"""Triage workflow — PRD §4.1.

v0.1 Week 1 vertical slice: only the ACK comment.

Full PRD §4.1 pipeline arrives in subsequent commits:
  1. Auto-label  (this commit: TODO marker only)
  2. Reproduce   (LLM-decided, Modal sandbox)
  3. Priority    (P0..P3 label)

Today: receive `issues.opened` → post an acknowledgement comment so the
reporter knows OpenBot saw the issue. Runs in FastAPI BackgroundTasks
*after* the webhook 202s, so a slow GitHub API doesn't make GitHub retry.

Failures here MUST NOT crash the webhook flow — they are logged at
ERROR level (PRD §9.4 audit_log surface) and swallowed.
"""

from __future__ import annotations

import logging

from openbot.adapters.github import GitHubAdapter
from openbot.events import EventKind, UnifiedEvent

_logger = logging.getLogger(__name__)

_ACK_TEMPLATE = (
    ":robot: Hi @{actor} — OpenBot received this issue and will triage shortly.\n\n"
    "_v0.1 alpha: only the ACK is automated so far. "
    "Auto-label, priority, and sandbox reproduce land in upcoming commits._"
)


async def maybe_run_triage(adapter: GitHubAdapter, event: UnifiedEvent) -> None:
    """Run the triage workflow if the event is a fresh issue.

    Args:
        adapter:  GitHubAdapter with `auth` set (needed for `reply`).
        event:    UnifiedEvent already produced by `adapter.parse_event`.

    Silent no-op if the event doesn't qualify; never raises.
    """
    if event.kind is not EventKind.ISSUE_OPENED:
        return

    if event.installation_id is None or event.issue_number is None:
        # Defensive: PRD §5.1 guarantees these for authentic issue events, but
        # log if a real webhook ever lacks them so we can spot it in audit.
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
        result = await adapter.reply(event, message)
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
        # Background-task failures must not leak as 500s on the webhook —
        # by the time this runs, GitHub already got its 202. Audit + drop.
        _logger.exception(
            "triage_ack_failed",
            extra={
                "delivery_id": event.delivery_id,
                "repo": event.repo,
                "issue_number": event.issue_number,
            },
        )
