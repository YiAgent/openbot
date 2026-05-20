"""Table-driven coverage of ``state.classifier.classify``.

One ``pytest.param`` per row of the intent matrix documented at the top
of ``openbot/application/state/classifier.py``. Each row exercises:

  * a synthetic ``UnifiedEvent`` (the minimum fields the classifier
    inspects — channel/repo/delivery_id are arbitrary)
  * a prior ``State`` value
  * the expected ``(intent, next_state[, reason])`` tuple

The classifier is pure: no fixtures, no I/O. The whole suite is one
parametrized function so a new matrix row is a one-line PR.
"""

from __future__ import annotations

import pytest

from openbot.application.state.classifier import classify
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.intents import Intent, State


def _event(
    kind: EventKind,
    *,
    actor_type: str = "User",
    comment_body: str | None = None,
    assignee_type: str | None = None,
    label_name: str | None = None,
    issue_number: int | None = 1,
    pr_number: int | None = None,
) -> UnifiedEvent:
    """Build the minimum ``UnifiedEvent`` the classifier inspects.

    Defaults to a human-authored issue event; per-case kwargs override
    the bits each row actually exercises.
    """
    raw: dict[str, object] = {}
    if assignee_type is not None:
        raw["assignee"] = {"type": assignee_type}
    if label_name is not None:
        raw["label"] = {"name": label_name}
    return UnifiedEvent(
        channel="github",
        delivery_id="d-1",
        kind=kind,
        repo="acme/openbot",
        actor="alice",
        actor_type=actor_type,
        issue_number=issue_number,
        pr_number=pr_number,
        comment_body=comment_body,
        raw=raw,
    )


# (event, prior_state, expected_intent, expected_next_state, expected_reason_or_None)
_CASES = [
    # ── cheap drops ──
    pytest.param(
        _event(EventKind.ISSUE_OPENED, actor_type="Bot"),
        State.IDLE,
        Intent.IGNORE,
        State.IDLE,
        "bot_actor",
        id="bot_author_ignored",
    ),
    pytest.param(
        _event(EventKind.UNKNOWN),
        State.IDLE,
        Intent.IGNORE,
        State.IDLE,
        "unknown_kind",
        id="unknown_kind_ignored",
    ),
    # ── issue.opened ──
    pytest.param(
        _event(EventKind.ISSUE_OPENED),
        State.IDLE,
        Intent.START,
        State.RUNNING,
        None,
        id="issue_opened_from_idle_starts",
    ),
    pytest.param(
        _event(EventKind.ISSUE_OPENED),
        State.CLOSED,
        Intent.START,
        State.RUNNING,
        None,
        id="issue_opened_from_closed_starts",
    ),
    pytest.param(
        _event(EventKind.ISSUE_OPENED),
        State.RUNNING,
        Intent.IGNORE,
        State.RUNNING,
        "already_running",
        id="issue_opened_while_running_ignored",
    ),
    # ── issue.edited ──
    pytest.param(
        _event(EventKind.ISSUE_EDITED),
        State.IDLE,
        Intent.START,
        State.RUNNING,
        None,
        id="issue_edited_from_idle_starts",
    ),
    pytest.param(
        _event(EventKind.ISSUE_EDITED),
        State.RUNNING,
        Intent.SUPERSEDE,
        State.RUNNING,
        None,
        id="issue_edited_while_running_supersedes",
    ),
    # ── issue.reopened ──
    pytest.param(
        _event(EventKind.ISSUE_REOPENED),
        State.CLOSED,
        Intent.START,
        State.RUNNING,
        None,
        id="issue_reopened_from_closed_starts",
    ),
    # ── issue.assigned ──
    pytest.param(
        _event(EventKind.ISSUE_ASSIGNED, assignee_type="Bot"),
        State.IDLE,
        Intent.START,
        State.RUNNING,
        None,
        id="assigned_to_bot_from_idle_starts",
    ),
    pytest.param(
        _event(EventKind.ISSUE_ASSIGNED, assignee_type="Bot"),
        State.RUNNING,
        Intent.SUPERSEDE,
        State.RUNNING,
        None,
        id="assigned_to_bot_while_running_supersedes",
    ),
    pytest.param(
        _event(EventKind.ISSUE_ASSIGNED, assignee_type="User"),
        State.IDLE,
        Intent.IGNORE,
        State.IDLE,
        "non_bot_assignee",
        id="assigned_to_human_ignored",
    ),
    # ── labeling ──
    pytest.param(
        _event(EventKind.ISSUE_LABELED, label_name="cancel-openbot"),
        State.RUNNING,
        Intent.CANCEL,
        State.CLOSED,
        "cancel_label_added",
        id="cancel_label_added_while_running_cancels",
    ),
    pytest.param(
        _event(EventKind.ISSUE_LABELED, label_name="bug"),
        State.RUNNING,
        Intent.IGNORE,
        State.RUNNING,
        "unrelated_label",
        id="unrelated_label_ignored",
    ),
    pytest.param(
        _event(EventKind.ISSUE_UNLABELED, label_name="cancel-openbot"),
        State.RUNNING,
        Intent.IGNORE,
        State.RUNNING,
        "label_removed",
        id="issue_unlabeled_returns_label_removed",
    ),
    pytest.param(
        _event(EventKind.ISSUE_UNLABELED),
        State.IDLE,
        Intent.IGNORE,
        State.IDLE,
        "label_removed",
        id="issue_unlabeled_from_idle_returns_label_removed",
    ),
    pytest.param(
        _event(
            EventKind.PR_UNLABELED, pr_number=42, issue_number=None, label_name="cancel-openbot"
        ),
        State.RUNNING,
        Intent.IGNORE,
        State.RUNNING,
        "label_removed",
        id="pr_unlabeled_returns_label_removed",
    ),
    pytest.param(
        _event(EventKind.PR_UNLABELED, pr_number=42, issue_number=None),
        State.IDLE,
        Intent.IGNORE,
        State.IDLE,
        "label_removed",
        id="pr_unlabeled_from_idle_returns_label_removed",
    ),
    # ── issue.closed / pr.closed / pr.merged ──
    pytest.param(
        _event(EventKind.ISSUE_CLOSED),
        State.RUNNING,
        Intent.CANCEL,
        State.CLOSED,
        "resource_closed",
        id="issue_closed_while_running_cancels",
    ),
    pytest.param(
        _event(EventKind.ISSUE_CLOSED),
        State.IDLE,
        Intent.IGNORE,
        State.CLOSED,
        "closed_no_run",
        id="issue_closed_from_idle_records_closed",
    ),
    pytest.param(
        _event(EventKind.PR_CLOSED, pr_number=42, issue_number=None),
        State.RUNNING,
        Intent.CANCEL,
        State.CLOSED,
        "resource_closed",
        id="pr_closed_while_running_cancels",
    ),
    pytest.param(
        _event(EventKind.PR_MERGED, pr_number=42, issue_number=None),
        State.RUNNING,
        Intent.CANCEL,
        State.CLOSED,
        "resource_closed",
        id="pr_merged_while_running_cancels",
    ),
    # ── pr.opened / pr.reopened / pr.synchronize ──
    pytest.param(
        _event(EventKind.PR_OPENED, pr_number=42, issue_number=None),
        State.IDLE,
        Intent.START,
        State.RUNNING,
        None,
        id="pr_opened_from_idle_starts",
    ),
    pytest.param(
        _event(EventKind.PR_OPENED, pr_number=42, issue_number=None),
        State.RUNNING,
        Intent.IGNORE,
        State.RUNNING,
        "already_running",
        id="pr_opened_while_running_ignored",
    ),
    pytest.param(
        _event(EventKind.PR_REOPENED, pr_number=42, issue_number=None),
        State.CLOSED,
        Intent.START,
        State.RUNNING,
        None,
        id="pr_reopened_from_closed_starts",
    ),
    pytest.param(
        _event(EventKind.PR_LABELED, pr_number=42, issue_number=None, label_name="cancel-openbot"),
        State.RUNNING,
        Intent.CANCEL,
        State.CLOSED,
        "cancel_label_added",
        id="pr_cancel_label_added_while_running_cancels",
    ),
    pytest.param(
        _event(EventKind.PR_SYNCHRONIZED, pr_number=42, issue_number=None),
        State.RUNNING,
        Intent.SUPERSEDE,
        State.RUNNING,
        None,
        id="pr_synchronize_while_running_supersedes",
    ),
    pytest.param(
        _event(EventKind.PR_SYNCHRONIZED, pr_number=42, issue_number=None),
        State.IDLE,
        Intent.START,
        State.RUNNING,
        None,
        id="pr_synchronize_from_idle_starts",
    ),
    # ── chat mentions ──
    pytest.param(
        _event(EventKind.ISSUE_COMMENT_CREATED, comment_body="@openbot help"),
        State.IDLE,
        Intent.START,
        State.RUNNING,
        None,
        id="chat_mention_from_idle_starts",
    ),
    pytest.param(
        _event(EventKind.ISSUE_COMMENT_CREATED, comment_body="@openbot help"),
        State.RUNNING,
        Intent.SUPERSEDE,
        State.RUNNING,
        None,
        id="chat_mention_while_running_supersedes",
    ),
    pytest.param(
        _event(EventKind.ISSUE_COMMENT_CREATED, comment_body="just chatting"),
        State.IDLE,
        Intent.IGNORE,
        State.IDLE,
        "no_chat_mention",
        id="no_mention_ignored",
    ),
    pytest.param(
        _event(
            EventKind.PR_REVIEW_COMMENT_CREATED,
            comment_body="@yibots take a look",
            pr_number=42,
            issue_number=None,
        ),
        State.RUNNING,
        Intent.SUPERSEDE,
        State.RUNNING,
        None,
        id="pr_review_mention_supersedes",
    ),
]


@pytest.mark.parametrize(("event", "prior_state", "intent", "next_state", "reason"), _CASES)
def test_classify_matrix(
    event: UnifiedEvent,
    prior_state: State,
    intent: Intent,
    next_state: State,
    reason: str | None,
) -> None:
    """Every documented (event, state) pair → expected EventClassification."""
    result = classify(event, prior_state)
    assert result.intent is intent
    assert result.next_state is next_state
    if reason is None:
        assert result.reason is None
    else:
        assert result.reason == reason
