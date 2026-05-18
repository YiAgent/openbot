"""Event classifier — pure ``(event, state) → EventClassification``.

No I/O. The classifier knows nothing about Redis / Postgres / asyncio —
it just turns the static event-kind by state grid from the plan into
runtime values. Persistence and ordering rules (stale-seq, CAS, lock)
live in ``runs_repo`` and ``webapp``.

Decision matrix mirrors the table in
``docs/superpowers/plans/2026-05-17-input-side-completeness.md``:

    | event.kind                          | current state | intent      |
    |-------------------------------------|---------------|-------------|
    | ISSUE_OPENED / ISSUE_REOPENED       | IDLE/CLOSED   | START       |
    | ISSUE_OPENED / ISSUE_REOPENED       | RUNNING       | IGNORE      |
    | ISSUE_ASSIGNED (to bot)             | IDLE/CLOSED   | START       |
    | ISSUE_ASSIGNED (to bot)             | RUNNING       | SUPERSEDE   |
    | ISSUE_CLOSED                        | RUNNING       | CANCEL      |
    | ISSUE_CLOSED                        | *             | IGNORE      |
    | ISSUE_COMMENT_CREATED (+@openbot)   | IDLE/CLOSED   | START       |
    | ISSUE_COMMENT_CREATED (+@openbot)   | RUNNING       | SUPERSEDE   |
    | ISSUE_COMMENT_CREATED (no @)        | *             | IGNORE      |
    | PR_OPENED / PR_REOPENED             | IDLE/CLOSED   | START       |
    | PR_SYNCHRONIZED                     | RUNNING       | SUPERSEDE   |
    | PR_SYNCHRONIZED                     | IDLE/CLOSED   | START       |
    | PR_CLOSED / PR_MERGED               | RUNNING       | CANCEL      |
    | PR_CLOSED / PR_MERGED               | *             | IGNORE      |
    | PR_REVIEW_COMMENT_CREATED (+@)      | IDLE/CLOSED   | START       |
    | PR_REVIEW_COMMENT_CREATED (+@)      | RUNNING       | SUPERSEDE   |
    | ping / unknown / bot author         | *             | IGNORE      |
"""

from __future__ import annotations

from typing import Final

from openbot.events import EventKind, UnifiedEvent
from openbot.state.intents import EventClassification, Intent, State

# The chat-mention prefixes the classifier accepts. Kept in sync with
# ``openbot.router._get_chat_prefix`` — when that grows to read settings,
# this should follow. For now both default to the same constants so chat
# routing is byte-identical between the legacy and state-machine paths.
_CHAT_PREFIXES: Final = ("@openbot ", "@yibots ")


def _is_chat_mention(event: UnifiedEvent) -> bool:
    body = event.comment_body or ""
    return any(body.startswith(prefix) for prefix in _CHAT_PREFIXES)


def _is_bot_assignee(event: UnifiedEvent) -> bool:
    """Mirror of ``Router._fix`` gate: only fire when the just-assigned actor
    is a Bot. PRD §4.3 trigger contract."""
    assignee = (event.raw.get("assignee") or {}) if event.raw else {}
    return assignee.get("type") == "Bot"


def classify(event: UnifiedEvent, current_state: State) -> EventClassification:
    """Map ``(event, current_state) → EventClassification``.

    Pure function. Bot authors and unknown kinds short-circuit to
    ``IGNORE`` first (the cheapest, most common drop). After that the
    decision is a flat match on ``event.kind`` paired with the four
    ``State`` values.

    The function does NOT inspect ``event.event_seq`` — stale-seq
    detection happens in the runs repo where the persisted high-water
    mark is available. Keeping that out of the classifier means the
    pure-function tests don't need a SQLAlchemy session.
    """
    # ── cheap drops ──
    if event.is_from_bot:
        return EventClassification(
            intent=Intent.IGNORE, next_state=current_state, reason="bot_actor"
        )
    if event.kind is EventKind.UNKNOWN:
        return EventClassification(
            intent=Intent.IGNORE, next_state=current_state, reason="unknown_kind"
        )

    kind = event.kind

    # ── open / reopen ──
    if kind in (EventKind.ISSUE_OPENED, EventKind.ISSUE_REOPENED):
        if current_state is State.RUNNING:
            # Duplicate / retried "opened" while a run is in flight — drop.
            return EventClassification(
                intent=Intent.IGNORE, next_state=State.RUNNING, reason="already_running"
            )
        return EventClassification(intent=Intent.START, next_state=State.RUNNING)

    if kind in (EventKind.PR_OPENED, EventKind.PR_REOPENED):
        if current_state is State.RUNNING:
            return EventClassification(
                intent=Intent.IGNORE, next_state=State.RUNNING, reason="already_running"
            )
        return EventClassification(intent=Intent.START, next_state=State.RUNNING)

    # ── assignment ──
    if kind is EventKind.ISSUE_ASSIGNED:
        if not _is_bot_assignee(event):
            return EventClassification(
                intent=Intent.IGNORE, next_state=current_state, reason="non_bot_assignee"
            )
        if current_state is State.RUNNING:
            return EventClassification(intent=Intent.SUPERSEDE, next_state=State.RUNNING)
        return EventClassification(intent=Intent.START, next_state=State.RUNNING)

    # ── close / merge ──
    if kind in (EventKind.ISSUE_CLOSED, EventKind.PR_CLOSED, EventKind.PR_MERGED):
        if current_state is State.RUNNING:
            return EventClassification(
                intent=Intent.CANCEL, next_state=State.CLOSED, reason="resource_closed"
            )
        # Already idle/closed — no run to cancel, but record the closed state
        # so a later REOPENED transitions correctly.
        return EventClassification(
            intent=Intent.IGNORE, next_state=State.CLOSED, reason="closed_no_run"
        )

    # ── synchronize (new commit) ──
    if kind is EventKind.PR_SYNCHRONIZED:
        if current_state is State.RUNNING:
            return EventClassification(intent=Intent.SUPERSEDE, next_state=State.RUNNING)
        return EventClassification(intent=Intent.START, next_state=State.RUNNING)

    # ── chat comments ──
    if kind in (EventKind.ISSUE_COMMENT_CREATED, EventKind.PR_REVIEW_COMMENT_CREATED):
        if not _is_chat_mention(event):
            return EventClassification(
                intent=Intent.IGNORE, next_state=current_state, reason="no_chat_mention"
            )
        if current_state is State.RUNNING:
            return EventClassification(intent=Intent.SUPERSEDE, next_state=State.RUNNING)
        return EventClassification(intent=Intent.START, next_state=State.RUNNING)

    # Defensive: any kind not enumerated above falls through to IGNORE.
    # In practice all EventKind values are covered, but the catch-all
    # keeps the function total over the enum.
    return EventClassification(
        intent=Intent.IGNORE, next_state=current_state, reason="unhandled_kind"
    )
