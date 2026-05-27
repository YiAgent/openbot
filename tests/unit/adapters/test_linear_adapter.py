"""Tests for LinearAdapter — webhook verification + event parsing."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from openbot.domain.events import EventKind
from openbot.infrastructure.adapters.base import SignatureError
from openbot.infrastructure.adapters.linear import LinearAdapter

_SECRET = "test-webhook-secret"


@pytest.fixture
def adapter() -> LinearAdapter:
    return LinearAdapter(webhook_secret=_SECRET, oauth_token="test-token")


def _sign(body: bytes) -> str:
    return hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()


# ── verify_signature ─────────────────────────────────────────────────────


def test_verify_signature_valid(adapter: LinearAdapter) -> None:
    body = b'{"action":"Issue.create","data":{}}'
    sig = _sign(body)
    # Should not raise
    adapter.verify_signature(body, {"linear-signature": sig})


def test_verify_signature_missing_header(adapter: LinearAdapter) -> None:
    with pytest.raises(SignatureError, match="Missing"):
        adapter.verify_signature(b"{}", {})


def test_verify_signature_invalid(adapter: LinearAdapter) -> None:
    with pytest.raises(SignatureError, match="Invalid"):
        adapter.verify_signature(b"{}", {"linear-signature": "bad"})


# ── parse_event ──────────────────────────────────────────────────────────


def test_parse_issue_create(adapter: LinearAdapter) -> None:
    body = json.dumps(
        {
            "action": "Issue.create",
            "webhookId": "wh-123",
            "data": {
                "id": "issue-abc",
                "identifier": "ENG-42",
                "title": "Bug report",
                "creator": {"name": "Alice"},
                "team": {"key": "ENG"},
            },
        }
    ).encode()

    event = adapter.parse_event(body, {})

    assert event.channel == "linear"
    assert event.kind == EventKind.ISSUE_OPENED
    assert event.issue_number == 42
    assert event.repo == "ENG"
    assert event.actor == "Alice"
    assert event.delivery_id == "wh-123"


def test_parse_issue_comment(adapter: LinearAdapter) -> None:
    body = json.dumps(
        {
            "action": "IssueComment.create",
            "webhookId": "wh-456",
            "data": {
                "id": "comment-xyz",
                "body": "@openbot help me fix this",
                "issue": {"identifier": "ENG-99"},
                "user": {"name": "Bob"},
                "team": {"key": "ENG"},
            },
        }
    ).encode()

    event = adapter.parse_event(body, {})

    assert event.kind == EventKind.ISSUE_COMMENT_CREATED
    assert event.comment_body == "@openbot help me fix this"


def test_parse_unknown_action(adapter: LinearAdapter) -> None:
    body = json.dumps(
        {
            "action": "Cycle.create",
            "webhookId": "wh-789",
            "data": {},
        }
    ).encode()

    event = adapter.parse_event(body, {})
    assert event.kind == EventKind.UNKNOWN


# ── stub methods ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_issue_labels_returns_empty(adapter: LinearAdapter) -> None:
    from openbot.domain.events import UnifiedEvent

    event = UnifiedEvent(
        channel="linear",
        delivery_id="d",
        kind=EventKind.ISSUE_OPENED,
        repo="ENG",
        actor="test",
    )
    result = await adapter.get_issue_labels(event, 1)
    assert result == frozenset()


@pytest.mark.asyncio
async def test_get_pr_comments_returns_empty(adapter: LinearAdapter) -> None:
    from openbot.domain.events import UnifiedEvent

    event = UnifiedEvent(
        channel="linear",
        delivery_id="d",
        kind=EventKind.ISSUE_OPENED,
        repo="ENG",
        actor="test",
    )
    result = await adapter.get_pr_comments(event, 1)
    assert result == []
