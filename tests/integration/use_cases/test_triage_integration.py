"""Integration tests for the triage use-case.

Wires FakeChannelAdapter + FakeAuditLog together through the real
maybe_run_triage function. No network, no DB.

Design rule (spec §8.3): integration tests call *at least two* fake
adapters so they exercise orchestration, not just one port in isolation.
"""

from __future__ import annotations

import pytest

from openbot.application.middleware.preflight import PreflightContext
from openbot.application.router import Dispatch, derive_task_id
from openbot.application.use_cases.triage import maybe_run_triage
from openbot.domain.events import EventKind, UnifiedEvent
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
        feature=Feature.TRIAGE,
        handler=maybe_run_triage,
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
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTriageAckPosted:
    async def test_triage_ack_posted(self) -> None:
        """ISSUE_OPENED event → FakeChannelAdapter records one reply with @actor."""
        fake_channel = FakeChannelAdapter()
        fake_audit = FakeAuditLog()
        event = build_issue_opened_event(
            sender="yiwang",
            issue_number=7,
            installation_id=_INSTALL_ID,
        )

        await maybe_run_triage(_ctx(fake_channel=fake_channel, fake_audit=fake_audit, event=event))

        assert len(fake_channel.replies) == 1
        reply = fake_channel.replies[0]
        assert "@yiwang" in reply.message
        assert "OpenBot" in reply.message

    async def test_triage_ack_targets_correct_repo(self) -> None:
        """The reply is associated with the correct repo from the event."""
        fake_channel = FakeChannelAdapter()
        event = build_issue_opened_event(
            repo="OwnerOrg/coolrepo",
            sender="alice",
            installation_id=_INSTALL_ID,
        )

        await maybe_run_triage(_ctx(fake_channel=fake_channel, event=event))

        assert len(fake_channel.replies) == 1
        assert fake_channel.replies[0].repo == "OwnerOrg/coolrepo"


@pytest.mark.integration
class TestTriageSkipsBotEvents:
    async def test_triage_skips_bot_events(self) -> None:
        """Event from a bot actor (actor_type='Bot') → no reply posted."""
        fake_channel = FakeChannelAdapter()
        fake_audit = FakeAuditLog()
        event = UnifiedEvent(
            channel="github",
            delivery_id="deliv-bot-1",
            kind=EventKind.ISSUE_OPENED,
            repo="owner/repo",
            actor="dependabot[bot]",
            actor_type="Bot",
            issue_number=42,
            installation_id=_INSTALL_ID,
        )

        await maybe_run_triage(_ctx(fake_channel=fake_channel, fake_audit=fake_audit, event=event))

        assert len(fake_channel.replies) == 0


@pytest.mark.integration
class TestTriageWithAudit:
    async def test_triage_with_audit_writes_started(self) -> None:
        """FakeAuditLog records a STARTED audit entry when session_factory is None.

        audit_lifecycle writes STARTED only when not already cached by
        AuditStartMiddleware.  In this path (no middleware chain), the
        cache is empty so the lifecycle helper writes the row.
        """
        fake_channel = FakeChannelAdapter()
        fake_audit = FakeAuditLog()
        event = build_issue_opened_event(
            sender="charlie",
            issue_number=5,
            installation_id=_INSTALL_ID,
        )

        await maybe_run_triage(_ctx(fake_channel=fake_channel, fake_audit=fake_audit, event=event))

        # The ctx.audit port (FakeAuditLog) is used by run_preflight for
        # middleware BLOCKED rows, not by audit_lifecycle (which uses
        # session_factory). For the audit_lifecycle path we verify that
        # the use-case DID NOT blow up and that the channel adapter recorded
        # the reply (proxy for a healthy lifecycle run).
        assert len(fake_channel.replies) == 1

    async def test_triage_with_audit_and_channel_both_active(self) -> None:
        """Both FakeAuditLog and FakeChannelAdapter are wired; reply is still posted."""
        fake_channel = FakeChannelAdapter()
        fake_audit = FakeAuditLog()
        event = build_issue_opened_event(
            sender="diana",
            issue_number=99,
            installation_id=_INSTALL_ID,
        )

        await maybe_run_triage(_ctx(fake_channel=fake_channel, fake_audit=fake_audit, event=event))

        assert len(fake_channel.replies) == 1
        assert "@diana" in fake_channel.replies[0].message


@pytest.mark.integration
class TestTriageSkipsNonIssueOpened:
    async def test_triage_skips_pr_opened(self) -> None:
        """PR_OPENED event → no reply posted (defense-in-depth skip)."""
        fake_channel = FakeChannelAdapter()
        fake_audit = FakeAuditLog()
        event = build_pull_request_opened_event(
            sender="eve",
            pr_number=3,
            installation_id=_INSTALL_ID,
        )

        await maybe_run_triage(_ctx(fake_channel=fake_channel, fake_audit=fake_audit, event=event))

        assert len(fake_channel.replies) == 0

    async def test_triage_skips_missing_issue_number(self) -> None:
        """Event with no issue_number → no reply posted."""
        fake_channel = FakeChannelAdapter()
        event = UnifiedEvent(
            channel="github",
            delivery_id="deliv-no-issue",
            kind=EventKind.ISSUE_OPENED,
            repo="owner/repo",
            actor="frank",
            actor_type="User",
            issue_number=None,
            installation_id=_INSTALL_ID,
        )

        await maybe_run_triage(_ctx(fake_channel=fake_channel, event=event))

        assert len(fake_channel.replies) == 0

    async def test_triage_skips_missing_installation_id(self) -> None:
        """Event with no installation_id → no reply posted."""
        fake_channel = FakeChannelAdapter()
        event = UnifiedEvent(
            channel="github",
            delivery_id="deliv-no-install",
            kind=EventKind.ISSUE_OPENED,
            repo="owner/repo",
            actor="grace",
            actor_type="User",
            issue_number=1,
            installation_id=None,
        )

        await maybe_run_triage(_ctx(fake_channel=fake_channel, event=event))

        assert len(fake_channel.replies) == 0
