"""Integration tests for the review use-case.

Wires FakeChannelAdapter + FakeAuditLog together through the real
maybe_run_review function. Avoids LLM calls by monkeypatching the
module-level ``_generate_review_findings`` seam documented in review.py.

Design rule (spec §8.3): integration tests call *at least two* fake
adapters so they exercise orchestration, not just one port in isolation.
"""

from __future__ import annotations

import pytest

from openbot.application.middleware.preflight import PreflightContext
from openbot.application.router import Dispatch, derive_task_id
from openbot.application.use_cases.review import maybe_run_review
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.review import ReviewFindings
from openbot.domain.workflows import Feature
from openbot.infrastructure.config_loader import baked_in_defaults
from openbot.testing.builders.events import (
    build_issue_opened_event,
    build_pull_request_opened_event,
)
from openbot.testing.fakes import FakeAuditLog, FakeChannelAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INSTALL_ID = 132_536_131


def _ctx(
    *,
    fake_channel: FakeChannelAdapter,
    fake_audit: FakeAuditLog | None = None,
    event: UnifiedEvent,
) -> PreflightContext:
    """Build a PreflightContext wired with real fakes for integration tests."""
    dispatch = Dispatch(
        feature=Feature.REVIEW,
        handler=maybe_run_review,
        task_id=derive_task_id(event),
    )
    return PreflightContext(
        event=event,
        dispatch=dispatch,
        config=baked_in_defaults(),
        adapter=fake_channel,
        session_factory=None,
        redis=None,
        audit=fake_audit,
        agent_checkpointer=None,
    )


def _empty_findings() -> ReviewFindings:
    """Return a ReviewFindings with no findings and a stub summary."""
    return ReviewFindings(findings=(), summary="No issues found.")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestReviewCallable:
    async def test_review_function_is_callable_with_preflight_context(self) -> None:
        """maybe_run_review accepts a PreflightContext and returns without error."""
        fake_channel = FakeChannelAdapter()
        fake_audit = FakeAuditLog()
        # PR_OPENED event with a valid pr_number so the skip guard is bypassed
        event = build_pull_request_opened_event(
            sender="alice",
            pr_number=1,
            installation_id=_INSTALL_ID,
        )

        # Monkeypatch the LLM seam so the test never makes a real agent call.
        import openbot.application.use_cases.review as review_mod

        async def _fake_generate(**_kwargs: object) -> ReviewFindings:
            return _empty_findings()

        original = review_mod._generate_review_findings
        review_mod._generate_review_findings = _fake_generate  # type: ignore[assignment]
        try:
            await maybe_run_review(
                _ctx(fake_channel=fake_channel, fake_audit=fake_audit, event=event)
            )
        finally:
            review_mod._generate_review_findings = original  # type: ignore[assignment]

        # An APPROVE review is posted when there are no findings.
        assert len(fake_channel.posted_reviews) == 1
        assert fake_channel.posted_reviews[0].event_type == "APPROVE"

    async def test_review_with_channel_and_audit_both_active(self) -> None:
        """Both FakeChannelAdapter and FakeAuditLog are wired; review is still posted."""
        fake_channel = FakeChannelAdapter()
        fake_audit = FakeAuditLog()
        event = build_pull_request_opened_event(
            sender="bob",
            pr_number=2,
            installation_id=_INSTALL_ID,
        )

        import openbot.application.use_cases.review as review_mod

        async def _fake_generate(**_kwargs: object) -> ReviewFindings:
            return _empty_findings()

        original = review_mod._generate_review_findings
        review_mod._generate_review_findings = _fake_generate  # type: ignore[assignment]
        try:
            await maybe_run_review(
                _ctx(fake_channel=fake_channel, fake_audit=fake_audit, event=event)
            )
        finally:
            review_mod._generate_review_findings = original  # type: ignore[assignment]

        assert len(fake_channel.posted_reviews) == 1


@pytest.mark.integration
class TestReviewSkipsNonPREvents:
    async def test_review_skips_issue_opened(self) -> None:
        """ISSUE_OPENED has no pr_number → maybe_run_review is a no-op."""
        fake_channel = FakeChannelAdapter()
        fake_audit = FakeAuditLog()
        event = build_issue_opened_event(
            sender="charlie",
            issue_number=10,
            installation_id=_INSTALL_ID,
        )

        await maybe_run_review(_ctx(fake_channel=fake_channel, fake_audit=fake_audit, event=event))

        # No PR review should have been submitted.
        assert len(fake_channel.posted_reviews) == 0

    async def test_review_skips_event_with_no_pr_number(self) -> None:
        """UnifiedEvent with pr_number=None → skip guard fires, no review posted."""
        fake_channel = FakeChannelAdapter()
        event = UnifiedEvent(
            channel="github",
            delivery_id="deliv-no-pr",
            kind=EventKind.PR_OPENED,
            repo="owner/repo",
            actor="diana",
            actor_type="User",
            pr_number=None,
            installation_id=_INSTALL_ID,
        )

        await maybe_run_review(_ctx(fake_channel=fake_channel, event=event))

        assert len(fake_channel.posted_reviews) == 0

    async def test_review_skips_event_with_no_installation_id(self) -> None:
        """UnifiedEvent with installation_id=None → skip guard fires, no review posted."""
        fake_channel = FakeChannelAdapter()
        event = UnifiedEvent(
            channel="github",
            delivery_id="deliv-no-install",
            kind=EventKind.PR_OPENED,
            repo="owner/repo",
            actor="eve",
            actor_type="User",
            pr_number=5,
            installation_id=None,
        )

        await maybe_run_review(_ctx(fake_channel=fake_channel, event=event))

        assert len(fake_channel.posted_reviews) == 0
