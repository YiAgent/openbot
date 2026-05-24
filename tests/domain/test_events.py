"""UnifiedEvent — domain-level shape tests.

These tests pin the *defaults* of optional fields introduced by the unified
sandbox entry design (Task 1.8 of plan
``docs/superpowers/plans/2026-05-21-unified-sandbox-entry-plan.md``).

The new fields are populated by adapters (``clone_url``, ``review_commit_id``)
or hydrated from the DB during dispatch (``last_reviewed_sha``); the domain
type only guarantees they default to ``None`` so existing constructors keep
working.
"""

from __future__ import annotations

from openbot.domain.events import EventKind, UnifiedEvent


def _minimal_event() -> UnifiedEvent:
    """An ``UnifiedEvent`` built from the *required* fields only.

    Used to verify that newly-added optional fields default to ``None`` —
    if a default were missing, this constructor would raise ``TypeError``.
    """
    return UnifiedEvent(
        channel="github",
        delivery_id="d-1",
        kind=EventKind.PR_OPENED,
        repo="acme/widget",
        actor="alice",
    )


def test_unified_event_clone_url_default_is_none() -> None:
    assert _minimal_event().clone_url is None


def test_unified_event_review_commit_id_default_is_none() -> None:
    assert _minimal_event().review_commit_id is None


def test_unified_event_last_reviewed_sha_default_is_none() -> None:
    assert _minimal_event().last_reviewed_sha is None


def test_unified_event_clone_url_round_trips() -> None:
    event = UnifiedEvent(
        channel="github",
        delivery_id="d-2",
        kind=EventKind.PR_OPENED,
        repo="acme/widget",
        actor="alice",
        clone_url="https://github.com/acme/widget.git",
    )
    assert event.clone_url == "https://github.com/acme/widget.git"


def test_unified_event_review_commit_id_round_trips() -> None:
    event = UnifiedEvent(
        channel="github",
        delivery_id="d-3",
        kind=EventKind.PR_REVIEW_COMMENT_CREATED,
        repo="acme/widget",
        actor="reviewer",
        review_commit_id="deadbee" + "f" * 33,
    )
    assert event.review_commit_id == "deadbee" + "f" * 33


def test_unified_event_last_reviewed_sha_round_trips() -> None:
    event = UnifiedEvent(
        channel="github",
        delivery_id="d-4",
        kind=EventKind.PR_SYNCHRONIZED,
        repo="acme/widget",
        actor="ci",
        last_reviewed_sha="cafef00d" + "0" * 32,
    )
    assert event.last_reviewed_sha == "cafef00d" + "0" * 32
