"""Unit tests for openbot.domain.events.

Tests: EventKind coverage, UnifiedEvent properties (is_relevant,
is_from_bot, resource_key), and immutability (frozen dataclass).
No IO, no fakes, no fixtures — pure functions only.
"""

from __future__ import annotations

import pytest

from openbot.domain.events import EventKind, UnifiedEvent


def _event(**kwargs) -> UnifiedEvent:
    """Minimal-defaults UnifiedEvent factory."""
    defaults: dict = {
        "channel": "github",
        "delivery_id": "d1",
        "kind": EventKind.ISSUE_OPENED,
        "repo": "owner/repo",
        "actor": "octocat",
    }
    defaults.update(kwargs)
    return UnifiedEvent(**defaults)


# ── EventKind ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestEventKind:
    def test_all_kinds_are_strings(self) -> None:
        for kind in EventKind:
            assert isinstance(kind.value, str)

    def test_unknown_is_sentinel(self) -> None:
        assert EventKind.UNKNOWN == "unknown"

    def test_cancel_kinds_present(self) -> None:
        cancel_kinds = {EventKind.ISSUE_CLOSED, EventKind.PR_CLOSED, EventKind.PR_MERGED}
        assert cancel_kinds.issubset(set(EventKind))


# ── UnifiedEvent.is_relevant ──────────────────────────────────────────────────


@pytest.mark.unit
class TestIsRelevant:
    def test_known_kind_is_relevant(self) -> None:
        assert _event(kind=EventKind.ISSUE_OPENED).is_relevant is True

    def test_unknown_kind_is_not_relevant(self) -> None:
        assert _event(kind=EventKind.UNKNOWN).is_relevant is False

    @pytest.mark.parametrize(
        "kind",
        [k for k in EventKind if k is not EventKind.UNKNOWN],
    )
    def test_every_non_unknown_kind_is_relevant(self, kind: EventKind) -> None:
        assert _event(kind=kind).is_relevant is True


# ── UnifiedEvent.is_from_bot ──────────────────────────────────────────────────


@pytest.mark.unit
class TestIsFromBot:
    def test_bot_actor_type(self) -> None:
        assert _event(actor_type="Bot").is_from_bot is True

    def test_user_actor_type(self) -> None:
        assert _event(actor_type="User").is_from_bot is False

    def test_none_actor_type(self) -> None:
        assert _event(actor_type=None).is_from_bot is False

    def test_organization_actor_type(self) -> None:
        assert _event(actor_type="Organization").is_from_bot is False


# ── UnifiedEvent.resource_key ─────────────────────────────────────────────────


@pytest.mark.unit
class TestResourceKey:
    def test_pr_number_produces_pr_key(self) -> None:
        key = _event(pr_number=42).resource_key
        assert key == "github:owner/repo:pr:42"

    def test_issue_number_produces_issue_key(self) -> None:
        key = _event(issue_number=7).resource_key
        assert key == "github:owner/repo:issue:7"

    def test_pr_takes_precedence_over_issue(self) -> None:
        key = _event(pr_number=1, issue_number=2).resource_key
        assert key == "github:owner/repo:pr:1"

    def test_no_number_returns_none(self) -> None:
        assert _event().resource_key is None

    def test_channel_is_embedded_in_key(self) -> None:
        key = _event(channel="gitlab", pr_number=5).resource_key
        assert key is not None and key.startswith("gitlab:")


# ── Immutability ──────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestImmutability:
    def test_frozen_raises_on_mutation(self) -> None:
        evt = _event()
        with pytest.raises((AttributeError, TypeError)):
            evt.actor = "hacker"  # type: ignore[misc]
