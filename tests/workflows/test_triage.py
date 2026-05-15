"""Triage ACK workflow (PRD §4.1 vertical slice)."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openbot.events import EventKind, UnifiedEvent
from openbot.workflows.triage import maybe_run_triage

_INSTALL_ID = 132_536_131


def _event(
    *,
    kind: EventKind = EventKind.ISSUE_OPENED,
    issue_number: int | None = 7,
    installation_id: int | None = _INSTALL_ID,
    actor: str = "yiwang",
) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="deliv-1",
        kind=kind,
        repo="YiAgent/openbot",
        actor=actor,
        issue_number=issue_number,
        installation_id=installation_id,
    )


def _adapter() -> Any:
    """A minimal adapter stand-in with an async `reply` mock."""
    a = AsyncMock()
    a.reply = AsyncMock(return_value={"id": 9999, "html_url": "https://..."})
    return a


# ───── happy path ─────


async def test_acks_issue_opened() -> None:
    adapter = _adapter()
    await maybe_run_triage(adapter, _event())

    adapter.reply.assert_awaited_once()
    posted_event, posted_msg = adapter.reply.await_args.args
    assert posted_event.issue_number == 7
    assert "@yiwang" in posted_msg
    assert "OpenBot" in posted_msg
    assert "triage shortly" in posted_msg


async def test_message_falls_back_when_actor_unknown() -> None:
    adapter = _adapter()
    await maybe_run_triage(adapter, _event(actor=""))

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
    ],
)
async def test_does_not_ack_other_event_kinds(kind: EventKind) -> None:
    adapter = _adapter()
    await maybe_run_triage(adapter, _event(kind=kind))
    adapter.reply.assert_not_awaited()


async def test_skips_when_installation_id_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter()
    with caplog.at_level(logging.WARNING, logger="openbot.workflows.triage"):
        await maybe_run_triage(adapter, _event(installation_id=None))
    adapter.reply.assert_not_awaited()
    assert any(r.message == "triage_skipped_missing_context" for r in caplog.records)


async def test_skips_when_issue_number_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter()
    with caplog.at_level(logging.WARNING, logger="openbot.workflows.triage"):
        await maybe_run_triage(adapter, _event(issue_number=None))
    adapter.reply.assert_not_awaited()
    assert any(r.message == "triage_skipped_missing_context" for r in caplog.records)


# ───── failure handling ─────


async def test_reply_failure_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter()
    adapter.reply = AsyncMock(side_effect=RuntimeError("boom"))

    with caplog.at_level(logging.ERROR, logger="openbot.workflows.triage"):
        # Must NOT raise — background tasks that 500 would be invisible to GitHub
        # but visible as nasty traceback in logs every time. Audit + drop instead.
        await maybe_run_triage(adapter, _event())

    assert any(r.message == "triage_ack_failed" for r in caplog.records)
    assert any(r.exc_info is not None for r in caplog.records if r.message == "triage_ack_failed")
