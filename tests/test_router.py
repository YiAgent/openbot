"""Router — pure dispatch + task_id derivation (harness spec §3 M2, §9.1)."""

from __future__ import annotations

import pytest

from openbot.events import EventKind, UnifiedEvent
from openbot.llm.router import Feature
from openbot.router import _CHAT_PREFIX, derive_task_id, dispatch_for
from openbot.workflows import maybe_run_chat, maybe_run_fix, maybe_run_review, maybe_run_triage


def _event(
    *,
    kind: EventKind = EventKind.ISSUE_OPENED,
    issue_number: int | None = 7,
    pr_number: int | None = None,
    installation_id: int | None = 100,
    actor_type: str | None = "User",
    comment_body: str | None = None,
    delivery_id: str = "deliv-1",
    repo: str = "YiAgent/openbot",
    channel: str = "github",
) -> UnifiedEvent:
    return UnifiedEvent(
        channel=channel,
        delivery_id=delivery_id,
        kind=kind,
        repo=repo,
        actor="u",
        actor_type=actor_type,
        issue_number=issue_number,
        pr_number=pr_number,
        installation_id=installation_id,
        comment_body=comment_body,
    )


# ───── task_id (§9.1) ─────


def test_task_id_is_32_hex_chars() -> None:
    tid = derive_task_id(_event())
    assert len(tid) == 32
    assert all(c in "0123456789abcdef" for c in tid)


def test_task_id_deterministic_for_same_event() -> None:
    e = _event()
    assert derive_task_id(e) == derive_task_id(e)


def test_task_id_changes_with_delivery_id() -> None:
    a = derive_task_id(_event(delivery_id="A"))
    b = derive_task_id(_event(delivery_id="B"))
    assert a != b


def test_task_id_changes_with_repo() -> None:
    a = derive_task_id(_event(repo="org/a"))
    b = derive_task_id(_event(repo="org/b"))
    assert a != b


def test_task_id_changes_with_channel() -> None:
    a = derive_task_id(_event(channel="github"))
    b = derive_task_id(_event(channel="linear"))
    assert a != b


def test_task_id_fits_cost_meter_column() -> None:
    """cost_meter.task_id is VARCHAR(64) — 32 hex chars leaves plenty of room."""
    assert len(derive_task_id(_event())) <= 64


# ───── ISSUE_OPENED → triage ─────


def test_dispatches_issue_opened_to_triage() -> None:
    d = dispatch_for(_event(kind=EventKind.ISSUE_OPENED))
    assert d is not None
    assert d.feature is Feature.TRIAGE
    assert d.handler is maybe_run_triage


def test_skips_bot_authored_issue() -> None:
    d = dispatch_for(_event(kind=EventKind.ISSUE_OPENED, actor_type="Bot"))
    assert d is None


def test_skips_issue_missing_installation_id() -> None:
    d = dispatch_for(_event(kind=EventKind.ISSUE_OPENED, installation_id=None))
    assert d is None


def test_skips_issue_missing_issue_number() -> None:
    d = dispatch_for(_event(kind=EventKind.ISSUE_OPENED, issue_number=None))
    assert d is None


# ───── PR_OPENED / PR_SYNCHRONIZED → review ─────


@pytest.mark.parametrize("kind", [EventKind.PR_OPENED, EventKind.PR_SYNCHRONIZED])
def test_dispatches_pr_to_review(kind: EventKind) -> None:
    d = dispatch_for(_event(kind=kind, issue_number=None, pr_number=42))
    assert d is not None
    assert d.feature is Feature.REVIEW
    assert d.handler is maybe_run_review


def test_skips_pr_missing_pr_number() -> None:
    d = dispatch_for(_event(kind=EventKind.PR_OPENED, issue_number=None, pr_number=None))
    assert d is None


# ───── ISSUE_ASSIGNED → fix ─────


def test_dispatches_issue_assigned_to_fix() -> None:
    d = dispatch_for(_event(kind=EventKind.ISSUE_ASSIGNED))
    assert d is not None
    assert d.feature is Feature.FIX
    assert d.handler is maybe_run_fix


# ───── @openbot comment → chat ─────


@pytest.mark.parametrize(
    "kind",
    [EventKind.ISSUE_COMMENT_CREATED, EventKind.PR_REVIEW_COMMENT_CREATED],
)
def test_dispatches_mention_comment_to_chat(kind: EventKind) -> None:
    d = dispatch_for(
        _event(
            kind=kind,
            comment_body=f"{_CHAT_PREFIX}help me reproduce this",
        )
    )
    assert d is not None
    assert d.feature is Feature.CHAT
    assert d.handler is maybe_run_chat


def test_skips_comment_without_mention() -> None:
    d = dispatch_for(
        _event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body="thanks!"),
    )
    assert d is None


def test_skips_comment_with_lookalike_login() -> None:
    """`@openbot-dev` is another bot's login — must NOT trigger."""
    d = dispatch_for(
        _event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body="@openbot-dev ping"),
    )
    assert d is None


def test_skips_comment_with_empty_body() -> None:
    d = dispatch_for(_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body=None))
    assert d is None


# ───── unknown event ─────


def test_returns_none_for_unknown_kind() -> None:
    d = dispatch_for(_event(kind=EventKind.UNKNOWN))
    assert d is None
