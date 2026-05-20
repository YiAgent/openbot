"""Tests for dispatcher/direct_actions.py — pure rule evaluation."""

from __future__ import annotations

from openbot.dispatcher.context import EventContext
from openbot.dispatcher.direct_actions import (
    PR_OVERSIZED_THRESHOLD,
    check_issue_completeness,
    check_mention_clarity,
    check_pr_size,
)


def _ctx(
    *,
    issue_body: str | None = None,
    issue_title: str | None = None,
    issue_labels: tuple[str, ...] = (),
    pr_additions: int = 0,
    pr_deletions: int = 0,
    pr_changed_files: int = 0,
    mention_body: str | None = None,
) -> EventContext:
    return EventContext(
        issue_body=issue_body,
        issue_title=issue_title,
        issue_labels=issue_labels,
        pr_additions=pr_additions,
        pr_deletions=pr_deletions,
        pr_changed_files=pr_changed_files,
        mention_body=mention_body,
    )


# ─── check_issue_completeness ─────────────────────────────────────────────────


class TestCheckIssueCompleteness:
    def test_absent_body_key_no_action(self) -> None:
        """body=None (absent key) → no action."""
        assert check_issue_completeness(_ctx(issue_body=None)) is None

    def test_body_with_content_no_action(self) -> None:
        assert check_issue_completeness(_ctx(issue_body="Describe the bug here")) is None

    def test_whitespace_only_body_triggers_action(self) -> None:
        action = check_issue_completeness(_ctx(issue_body="   \n  "))
        assert action is not None
        assert "needs-info" in action.labels_to_add
        assert action.drop is True

    def test_empty_string_body_triggers_action(self) -> None:
        action = check_issue_completeness(_ctx(issue_body=""))
        assert action is not None
        assert "needs-info" in action.labels_to_add

    def test_message_is_non_empty(self) -> None:
        action = check_issue_completeness(_ctx(issue_body=""))
        assert action is not None
        assert len(action.message) > 20


# ─── check_pr_size ────────────────────────────────────────────────────────────


class TestCheckPrSize:
    def test_small_pr_no_action(self) -> None:
        ctx = _ctx(pr_additions=100, pr_deletions=50)
        assert check_pr_size(ctx) is None

    def test_exactly_threshold_no_action(self) -> None:
        """Boundary: exactly at threshold → no action (> not >=)."""
        ctx = _ctx(pr_additions=PR_OVERSIZED_THRESHOLD, pr_deletions=0)
        assert check_pr_size(ctx) is None

    def test_one_over_threshold_triggers_action(self) -> None:
        ctx = _ctx(pr_additions=PR_OVERSIZED_THRESHOLD, pr_deletions=1)
        action = check_pr_size(ctx)
        assert action is not None
        assert action.drop is True

    def test_message_mentions_line_count(self) -> None:
        total = PR_OVERSIZED_THRESHOLD + 100
        ctx = _ctx(pr_additions=total, pr_deletions=0)
        action = check_pr_size(ctx)
        assert action is not None
        assert str(total) in action.message

    def test_no_labels_for_oversized_pr(self) -> None:
        ctx = _ctx(pr_additions=600, pr_deletions=0)
        action = check_pr_size(ctx)
        assert action is not None
        assert action.labels_to_add == []


# ─── check_mention_clarity ────────────────────────────────────────────────────


class TestCheckMentionClarity:
    def test_no_mention_no_action(self) -> None:
        assert check_mention_clarity(_ctx(mention_body=None)) is None

    def test_empty_mention_triggers_action(self) -> None:
        action = check_mention_clarity(_ctx(mention_body=""))
        assert action is not None

    def test_whitespace_mention_triggers_action(self) -> None:
        action = check_mention_clarity(_ctx(mention_body="   "))
        assert action is not None

    def test_mention_prefix_only_triggers_action(self) -> None:
        """Just "@openbot " with nothing after → vague."""
        action = check_mention_clarity(_ctx(mention_body="@openbot "))
        assert action is not None

    def test_short_mention_triggers_action(self) -> None:
        """Short body (< minimum chars after prefix strip) → vague."""
        action = check_mention_clarity(_ctx(mention_body="@openbot hi"))
        assert action is not None

    def test_substantive_mention_no_action(self) -> None:
        """Long enough body → no action, let the LLM handle it."""
        long_body = "@openbot can you triage this issue and assign the bug label please?"
        assert check_mention_clarity(_ctx(mention_body=long_body)) is None

    def test_direct_mention_without_prefix(self) -> None:
        """Body without @openbot prefix counted from start."""
        long_body = "This is a detailed request about the authentication system failing."
        assert check_mention_clarity(_ctx(mention_body=long_body)) is None
