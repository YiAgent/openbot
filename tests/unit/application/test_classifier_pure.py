"""ClassifierPolicy — deterministic per-feature routing rules."""

from __future__ import annotations

from openbot.application.state.classifier import classify_pure
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature


def _ev(kind: EventKind, comment_body: str = "") -> UnifiedEvent:
    return UnifiedEvent(
        delivery_id="00000000-0000-0000-0000-000000000001",
        channel="github",
        kind=kind,
        actor="octocat",
        repo="owner/repo",
        installation_id=1,
        comment_body=comment_body or None,
    )


class TestClassifyPure:
    def test_issue_opened_routes_to_triage(self) -> None:
        assert classify_pure(_ev(EventKind.ISSUE_OPENED)) == Feature.TRIAGE

    def test_issue_reopened_routes_to_triage(self) -> None:
        assert classify_pure(_ev(EventKind.ISSUE_REOPENED)) == Feature.TRIAGE

    def test_issue_assigned_routes_to_triage(self) -> None:
        assert classify_pure(_ev(EventKind.ISSUE_ASSIGNED)) == Feature.TRIAGE

    def test_pr_opened_routes_to_review(self) -> None:
        assert classify_pure(_ev(EventKind.PR_OPENED)) == Feature.REVIEW

    def test_pr_reopened_routes_to_review(self) -> None:
        assert classify_pure(_ev(EventKind.PR_REOPENED)) == Feature.REVIEW

    def test_pr_synchronized_routes_to_review(self) -> None:
        assert classify_pure(_ev(EventKind.PR_SYNCHRONIZED)) == Feature.REVIEW

    def test_chat_mention_routes_to_chat(self) -> None:
        assert (
            classify_pure(_ev(EventKind.ISSUE_COMMENT_CREATED, "@openbot summarize this"))
            == Feature.CHAT
        )

    def test_yibots_chat_mention_routes_to_chat(self) -> None:
        assert (
            classify_pure(_ev(EventKind.ISSUE_COMMENT_CREATED, "@yibots summarize this"))
            == Feature.CHAT
        )

    def test_fix_command_routes_to_fix(self) -> None:
        assert (
            classify_pure(_ev(EventKind.ISSUE_COMMENT_CREATED, "@openbot fix this")) == Feature.FIX
        )

    def test_yibots_fix_command_routes_to_fix(self) -> None:
        assert (
            classify_pure(_ev(EventKind.ISSUE_COMMENT_CREATED, "@yibots fix the bug"))
            == Feature.FIX
        )

    def test_pr_review_comment_chat_routes_to_chat(self) -> None:
        assert (
            classify_pure(_ev(EventKind.PR_REVIEW_COMMENT_CREATED, "@openbot explain this"))
            == Feature.CHAT
        )

    def test_unrelated_comment_returns_none(self) -> None:
        assert classify_pure(_ev(EventKind.ISSUE_COMMENT_CREATED, "lgtm")) is None

    def test_unknown_kind_returns_none(self) -> None:
        assert classify_pure(_ev(EventKind.UNKNOWN)) is None

    def test_bot_actor_returns_none(self) -> None:
        ev = UnifiedEvent(
            delivery_id="00000000-0000-0000-0000-000000000002",
            channel="github",
            kind=EventKind.ISSUE_OPENED,
            actor="openbot-dev[bot]",
            actor_type="Bot",
            repo="owner/repo",
            installation_id=1,
        )
        assert classify_pure(ev) is None

    def test_issue_closed_returns_none(self) -> None:
        assert classify_pure(_ev(EventKind.ISSUE_CLOSED)) is None

    def test_pr_closed_returns_none(self) -> None:
        assert classify_pure(_ev(EventKind.PR_CLOSED)) is None

    def test_empty_mention_body_returns_none(self) -> None:
        assert classify_pure(_ev(EventKind.ISSUE_COMMENT_CREATED)) is None
