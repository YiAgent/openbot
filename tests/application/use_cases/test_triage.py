"""Triage ACK workflow (PRD §4.1 vertical slice)."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openbot.application.middleware import PreflightContext
from openbot.application.router import Dispatch, derive_task_id
from openbot.application.use_cases.triage import maybe_run_triage
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from openbot.infrastructure.config_loader import baked_in_defaults

_INSTALL_ID = 132_536_131


def _event(
    *,
    kind: EventKind = EventKind.ISSUE_OPENED,
    issue_number: int | None = 7,
    installation_id: int | None = _INSTALL_ID,
    actor: str = "yiwang",
    actor_type: str | None = "User",
) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="deliv-1",
        kind=kind,
        repo="YiAgent/openbot",
        actor=actor,
        actor_type=actor_type,
        issue_number=issue_number,
        installation_id=installation_id,
    )


def _adapter() -> Any:
    """A minimal adapter stand-in with an async `reply` mock."""
    a = AsyncMock()
    a.reply = AsyncMock(return_value={"id": 9999, "html_url": "https://..."})
    return a


def _ctx(
    adapter: Any,
    event: UnifiedEvent,
    *,
    classifier_output: Any = None,
) -> PreflightContext:
    """Build a minimal PreflightContext for workflow stub tests.

    `session_factory=None` skips audit_log writes — those are exercised
    in tests/middleware/test_preflight_order.py. `redis=None` is fine for
    workflow tests; only middleware uses redis.
    """
    dispatch = Dispatch(
        feature=Feature.TRIAGE, handler=maybe_run_triage, task_id=derive_task_id(event)
    )
    return PreflightContext(
        event=event,
        dispatch=dispatch,
        config=baked_in_defaults(),
        adapter=adapter,
        session_factory=None,
        redis=None,
        classifier_output=classifier_output,
    )


# ───── happy path ─────


async def test_acks_issue_opened() -> None:
    adapter = _adapter()
    await maybe_run_triage(_ctx(adapter, _event()))

    adapter.reply.assert_awaited_once()
    posted_event, posted_msg = adapter.reply.await_args.args
    assert posted_event.issue_number == 7
    assert "@yiwang" in posted_msg
    assert "OpenBot" in posted_msg
    assert "triage shortly" in posted_msg


async def test_message_falls_back_when_actor_unknown() -> None:
    adapter = _adapter()
    await maybe_run_triage(_ctx(adapter, _event(actor="")))

    _, msg = adapter.reply.await_args.args
    # No "@" placeholder leftover; uses "there" as fallback.
    assert "@there" in msg or "Hi @there" in msg


# ───── skip conditions ─────


@pytest.mark.parametrize(
    "kind",
    [
        EventKind.PR_OPENED,
        EventKind.PR_SYNCHRONIZED,
        EventKind.ISSUE_ASSIGNED,
        EventKind.ISSUE_COMMENT_CREATED,
        EventKind.PR_REVIEW_COMMENT_CREATED,
        EventKind.UNKNOWN,
        # LABELED/UNLABELED reach the handler via the cancel-label router path;
        # they must be silent no-ops (cancel is handled by middleware, not here).
        EventKind.ISSUE_LABELED,
        EventKind.ISSUE_UNLABELED,
        # CLOSED reaches the handler so the state machine can fire the cancel
        # signal; no user-visible reply is expected.
        EventKind.ISSUE_CLOSED,
    ],
)
async def test_does_not_ack_other_event_kinds(kind: EventKind) -> None:
    adapter = _adapter()
    await maybe_run_triage(_ctx(adapter, _event(kind=kind)))
    adapter.reply.assert_not_awaited()


# ───── ISSUE_EDITED and ISSUE_REOPENED trigger ACK ─────


async def test_acks_issue_edited() -> None:
    adapter = _adapter()
    await maybe_run_triage(_ctx(adapter, _event(kind=EventKind.ISSUE_EDITED)))

    adapter.reply.assert_awaited_once()
    _, posted_msg = adapter.reply.await_args.args
    assert "OpenBot" in posted_msg


async def test_acks_issue_reopened() -> None:
    adapter = _adapter()
    await maybe_run_triage(_ctx(adapter, _event(kind=EventKind.ISSUE_REOPENED)))

    adapter.reply.assert_awaited_once()
    _, posted_msg = adapter.reply.await_args.args
    assert "OpenBot" in posted_msg


# ───── classifier output → labels ─────


async def test_applies_type_and_priority_label_when_classifier_output_present() -> None:
    """When classifier output is present, add_label is called with mapped labels."""
    from openbot.dispatcher.classifier import TriageClassifierOutput

    adapter = _adapter()
    adapter.add_label = AsyncMock(return_value=[])
    classifier = TriageClassifierOutput(
        type="bug",
        severity_guess="high",
        has_reproduction_info=False,
        looks_like_spam=False,
    )
    ctx = _ctx(adapter, _event(), classifier_output=classifier)
    await maybe_run_triage(ctx)

    adapter.reply.assert_awaited_once()
    adapter.add_label.assert_awaited_once()
    _, *label_args = adapter.add_label.await_args.args
    # Both type label and priority label should be applied.
    assert "bug" in label_args
    assert "priority:high" in label_args


async def test_no_label_call_when_classifier_output_absent() -> None:
    """Without a classifier output, add_label must not be called."""
    adapter = _adapter()
    adapter.add_label = AsyncMock(return_value=[])
    await maybe_run_triage(_ctx(adapter, _event()))
    adapter.add_label.assert_not_awaited()


async def test_label_failure_does_not_prevent_ack() -> None:
    """add_label failure is best-effort — the ACK reply must still land."""
    from openbot.dispatcher.classifier import TriageClassifierOutput

    adapter = _adapter()
    adapter.add_label = AsyncMock(side_effect=RuntimeError("404 label not found"))
    classifier = TriageClassifierOutput(
        type="feature",
        severity_guess="low",
        has_reproduction_info=False,
        looks_like_spam=False,
    )
    ctx = _ctx(adapter, _event(), classifier_output=classifier)
    # Must NOT raise — label failure is best-effort.
    await maybe_run_triage(ctx)
    adapter.reply.assert_awaited_once()


async def test_spam_type_skips_label() -> None:
    """'spam' type maps to the 'spam' label; 'other' type is skipped."""
    from openbot.dispatcher.classifier import TriageClassifierOutput

    adapter = _adapter()
    adapter.add_label = AsyncMock(return_value=[])
    classifier = TriageClassifierOutput(
        type="other",
        severity_guess="low",
        has_reproduction_info=False,
        looks_like_spam=False,
    )
    ctx = _ctx(adapter, _event(), classifier_output=classifier)
    await maybe_run_triage(ctx)
    # "other" has no mapped label, only "priority:low" should be added.
    adapter.add_label.assert_awaited_once()
    _, *label_args = adapter.add_label.await_args.args
    assert "other" not in label_args
    assert "priority:low" in label_args


async def test_skips_when_installation_id_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter()
    with caplog.at_level(logging.WARNING, logger="openbot.application.workflows.triage"):
        await maybe_run_triage(_ctx(adapter, _event(installation_id=None)))
    adapter.reply.assert_not_awaited()
    assert any(r.message == "triage_skipped_missing_context" for r in caplog.records)


async def test_skips_when_issue_number_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter()
    with caplog.at_level(logging.WARNING, logger="openbot.application.workflows.triage"):
        await maybe_run_triage(_ctx(adapter, _event(issue_number=None)))
    adapter.reply.assert_not_awaited()
    assert any(r.message == "triage_skipped_missing_context" for r in caplog.records)


async def test_does_not_ack_bot_authored_issue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bot-authored issues (dependabot, our own bot, etc.) get no ACK.

    Two reasons documented on the workflow:
      1. ACK'ing routine dependabot issues is noise without value.
      2. Defense-in-depth against any future workflow that opens issues
         under our own App identity — an ACK would echo-loop.
    """
    adapter = _adapter()
    bot_event = _event(actor="dependabot[bot]", actor_type="Bot")
    assert bot_event.is_from_bot is True  # sanity

    with caplog.at_level(logging.INFO, logger="openbot.application.use_cases.triage"):
        await maybe_run_triage(_ctx(adapter, bot_event))

    adapter.reply.assert_not_awaited()
    assert any(r.message == "triage_skipped_bot_actor" for r in caplog.records)


async def test_acks_human_authored_issue_with_explicit_user_type() -> None:
    """Belt-and-suspenders: events with actor_type='User' must still ACK."""
    adapter = _adapter()
    await maybe_run_triage(_ctx(adapter, _event(actor_type="User")))
    adapter.reply.assert_awaited_once()


async def test_acks_when_actor_type_missing() -> None:
    """Defensive: if actor_type is None (older / partial payload), do NOT
    over-skip — only Bot is skipped, not "unknown"."""
    adapter = _adapter()
    await maybe_run_triage(_ctx(adapter, _event(actor_type=None)))
    adapter.reply.assert_awaited_once()


# ───── failure handling ─────


async def test_reply_failure_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter()
    adapter.reply = AsyncMock(side_effect=RuntimeError("boom"))

    with caplog.at_level(logging.ERROR, logger="openbot.application.workflows.triage"):
        # Must NOT raise — background tasks that 500 would be invisible to GitHub
        # but visible as nasty traceback in logs every time. Audit + drop instead.
        await maybe_run_triage(_ctx(adapter, _event()))

    assert any(r.message == "triage_ack_failed" for r in caplog.records)
    assert any(r.exc_info is not None for r in caplog.records if r.message == "triage_ack_failed")
