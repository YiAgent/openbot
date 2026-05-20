"""Tests for dispatcher/context.py — pure EventContext extraction."""

from __future__ import annotations

from openbot.dispatcher.context import extract_event_context
from openbot.domain.events import EventKind, UnifiedEvent


def _event(raw: dict, comment_body: str | None = None) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="del-1",
        kind=EventKind.ISSUE_OPENED,
        repo="org/r",
        actor="alice",
        actor_type="User",
        issue_number=7,
        pr_number=None,
        installation_id=100,
        comment_body=comment_body,
        raw=raw,
    )


def test_empty_body_is_empty_string() -> None:
    """issue.body = "" (explicit empty) → issue_body == "" (not None)."""
    ev = _event({"issue": {"number": 7, "body": ""}})
    ctx = extract_event_context(ev)
    assert ctx.issue_body == ""


def test_null_body_is_none() -> None:
    """issue.body = null in JSON → issue_body is None."""
    ev = _event({"issue": {"number": 7, "body": None}})
    ctx = extract_event_context(ev)
    assert ctx.issue_body is None


def test_absent_body_key_is_none() -> None:
    """No 'body' key in issue dict → issue_body is None."""
    ev = _event({"issue": {"number": 7}})
    ctx = extract_event_context(ev)
    assert ctx.issue_body is None


def test_whitespace_only_body_is_not_empty() -> None:
    """Whitespace-only body is preserved as-is (callers strip if needed)."""
    ev = _event({"issue": {"number": 7, "body": "   "}})
    ctx = extract_event_context(ev)
    assert ctx.issue_body == "   "


def test_issue_labels_extracted() -> None:
    raw = {"issue": {"labels": [{"name": "bug"}, {"name": "triage"}]}}
    ctx = extract_event_context(_event(raw))
    assert ctx.issue_labels == ("bug", "triage")


def test_pr_stats_extracted() -> None:
    raw = {"pull_request": {"additions": 300, "deletions": 250, "changed_files": 12}}
    ctx = extract_event_context(_event(raw))
    assert ctx.pr_additions == 300
    assert ctx.pr_deletions == 250
    assert ctx.pr_changed_files == 12
    assert ctx.pr_total_lines_changed == 550


def test_empty_raw_gives_defaults() -> None:
    ctx = extract_event_context(_event({}))
    assert ctx.issue_body is None
    assert ctx.issue_labels == ()
    assert ctx.pr_total_lines_changed == 0


def test_mention_body_from_comment() -> None:
    ctx = extract_event_context(_event({}, comment_body="@openbot help"))
    assert ctx.mention_body == "@openbot help"


def test_mention_body_none_when_no_comment() -> None:
    ctx = extract_event_context(_event({}))
    assert ctx.mention_body is None
