"""Unit tests for openbot.application.router.dispatch_for.

Tests: event→feature routing, bot-authored suppression, None-returns
for incomplete events, and deterministic task_id derivation.
No IO, no fakes. Settings cache is cleared per-test so routing
is always based on the ambient (scrubbed) environment.
"""

from __future__ import annotations

import pytest

from openbot.application.router import SandboxPolicy, dispatch_for
from openbot.core.settings import get_settings
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Ensure dispatch_for reads a fresh Settings on each test."""
    get_settings.cache_clear()


def _evt(
    kind: EventKind,
    *,
    repo: str = "owner/repo",
    actor_type: str = "User",
    issue_number: int | None = None,
    pr_number: int | None = None,
    installation_id: int | None = 100,
    comment_body: str | None = None,
    raw: dict | None = None,
) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d1",
        kind=kind,
        repo=repo,
        actor="octocat",
        actor_type=actor_type,
        issue_number=issue_number,
        pr_number=pr_number,
        installation_id=installation_id,
        comment_body=comment_body,
        raw=raw or {},
    )


# ── Bot-event suppression ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestBotSuppression:
    def test_bot_actor_returns_none(self) -> None:
        evt = _evt(EventKind.ISSUE_OPENED, actor_type="Bot", issue_number=1)
        assert dispatch_for(evt) is None

    def test_unknown_kind_returns_none(self) -> None:
        evt = _evt(EventKind.UNKNOWN)
        assert dispatch_for(evt) is None


# ── Feature routing ───────────────────────────────────────────────────────────


@pytest.mark.unit
class TestFeatureRouting:
    @pytest.mark.parametrize(
        "kind",
        [EventKind.ISSUE_OPENED, EventKind.ISSUE_EDITED, EventKind.ISSUE_REOPENED],
    )
    def test_issue_events_route_to_triage(self, kind: EventKind) -> None:
        d = dispatch_for(_evt(kind, issue_number=1))
        assert d is not None
        assert d.feature is Feature.TRIAGE

    @pytest.mark.parametrize(
        "kind",
        [EventKind.PR_OPENED, EventKind.PR_SYNCHRONIZED, EventKind.PR_REOPENED],
    )
    def test_pr_events_route_to_review(self, kind: EventKind) -> None:
        d = dispatch_for(_evt(kind, pr_number=1))
        assert d is not None
        assert d.feature is Feature.REVIEW

    def test_issue_assigned_to_bot_routes_to_fix(self) -> None:
        evt = _evt(
            EventKind.ISSUE_ASSIGNED,
            issue_number=1,
            raw={"assignee": {"type": "Bot"}},
        )
        d = dispatch_for(evt)
        assert d is not None
        assert d.feature is Feature.FIX

    def test_issue_assigned_to_human_returns_none(self) -> None:
        evt = _evt(
            EventKind.ISSUE_ASSIGNED,
            issue_number=1,
            raw={"assignee": {"type": "User"}},
        )
        assert dispatch_for(evt) is None

    def test_at_mention_comment_routes_to_chat(self) -> None:
        evt = _evt(
            EventKind.ISSUE_COMMENT_CREATED,
            issue_number=1,
            comment_body="@openbot please summarize",
        )
        d = dispatch_for(evt)
        assert d is not None
        assert d.feature is Feature.CHAT

    def test_plain_comment_without_mention_returns_none(self) -> None:
        evt = _evt(
            EventKind.ISSUE_COMMENT_CREATED,
            issue_number=1,
            comment_body="LGTM",
        )
        assert dispatch_for(evt) is None


# ── Incomplete event guard ────────────────────────────────────────────────────


@pytest.mark.unit
class TestIncompleteEventGuard:
    def test_issue_opened_without_issue_number_returns_none(self) -> None:
        assert dispatch_for(_evt(EventKind.ISSUE_OPENED, issue_number=None)) is None

    def test_issue_opened_without_installation_id_returns_none(self) -> None:
        assert (
            dispatch_for(_evt(EventKind.ISSUE_OPENED, issue_number=1, installation_id=None)) is None
        )

    def test_pr_opened_without_pr_number_returns_none(self) -> None:
        assert dispatch_for(_evt(EventKind.PR_OPENED, pr_number=None)) is None


# ── Sandbox policy ────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestSandboxPolicy:
    def test_issue_opened_requires_sandbox(self) -> None:
        d = dispatch_for(_evt(EventKind.ISSUE_OPENED, issue_number=1))
        assert d is not None
        assert d.sandbox_policy is SandboxPolicy.REQUIRED

    def test_label_events_bypass_sandbox(self) -> None:
        d = dispatch_for(_evt(EventKind.ISSUE_LABELED, issue_number=1))
        assert d is not None
        assert d.sandbox_policy is SandboxPolicy.NO_SANDBOX


# ── Task ID determinism ───────────────────────────────────────────────────────


@pytest.mark.unit
class TestTaskId:
    def test_same_delivery_same_task_id(self) -> None:
        evt = _evt(EventKind.ISSUE_OPENED, issue_number=1)
        d1 = dispatch_for(evt)
        d2 = dispatch_for(evt)
        assert d1 is not None and d2 is not None
        assert d1.task_id == d2.task_id

    def test_task_id_is_32_hex_chars(self) -> None:
        d = dispatch_for(_evt(EventKind.ISSUE_OPENED, issue_number=1))
        assert d is not None
        assert len(d.task_id) == 32
        assert all(c in "0123456789abcdef" for c in d.task_id)
