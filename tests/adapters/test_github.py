"""GitHubAdapter — signature verification + payload parsing.

Covers PRD §4.8 (HMAC trust boundary) and PRD §5.1 (UnifiedEvent mapping).
"""

from __future__ import annotations

import hmac
import json
from hashlib import sha256

import pytest

from openbot.adapters.base import SignatureError
from openbot.adapters.github import GitHubAdapter
from openbot.events import EventKind

_SECRET = "test-secret"


def _sign(body: bytes, *, secret: str = _SECRET, event: str = "issues") -> dict[str, str]:
    sig = "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()
    return {
        "x-hub-signature-256": sig,
        "x-github-event": event,
        "x-github-delivery": "abc-123",
    }


def _adapter() -> GitHubAdapter:
    return GitHubAdapter(webhook_secret=_SECRET)


# ───── constructor ─────


def test_constructor_rejects_empty_secret() -> None:
    with pytest.raises(ValueError):
        GitHubAdapter(webhook_secret="")


# ───── verify_signature ─────


def test_verify_accepts_valid_signature() -> None:
    body = b'{"action":"opened"}'
    _adapter().verify_signature(body, _sign(body))


def test_verify_rejects_missing_header() -> None:
    with pytest.raises(SignatureError, match="missing or malformed"):
        _adapter().verify_signature(b"{}", {})


def test_verify_rejects_wrong_algorithm_prefix() -> None:
    with pytest.raises(SignatureError, match="missing or malformed"):
        _adapter().verify_signature(b"{}", {"x-hub-signature-256": "md5=deadbeef"})


def test_verify_rejects_tampered_body() -> None:
    body = b'{"action":"opened"}'
    headers = _sign(body)
    with pytest.raises(SignatureError, match="mismatch"):
        _adapter().verify_signature(b'{"action":"closed"}', headers)


def test_verify_rejects_wrong_secret() -> None:
    body = b"{}"
    bad_headers = _sign(body, secret="other-secret")
    with pytest.raises(SignatureError, match="mismatch"):
        _adapter().verify_signature(body, bad_headers)


# ───── parse_event ─────


def _issue_opened_payload() -> dict:
    return {
        "action": "opened",
        "issue": {"number": 42, "title": "Test bug"},
        "repository": {"full_name": "YiAgent/openbot"},
        "sender": {"login": "yiwang"},
    }


def test_parse_issue_opened() -> None:
    body = json.dumps(_issue_opened_payload()).encode()
    event = _adapter().parse_event(body, _sign(body, event="issues"))

    assert event.channel == "github"
    assert event.kind is EventKind.ISSUE_OPENED
    assert event.repo == "YiAgent/openbot"
    assert event.issue_number == 42
    assert event.pr_number is None
    assert event.actor == "yiwang"
    assert event.delivery_id == "abc-123"
    assert event.is_relevant


def test_parse_pull_request_opened() -> None:
    payload = {
        "action": "opened",
        "pull_request": {"number": 17},
        "repository": {"full_name": "YiAgent/openbot"},
        "sender": {"login": "contributor"},
    }
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body, event="pull_request"))

    assert event.kind is EventKind.PR_OPENED
    assert event.pr_number == 17
    assert event.issue_number is None


def test_parse_pull_request_synchronize() -> None:
    payload = {
        "action": "synchronize",
        "pull_request": {"number": 17},
        "repository": {"full_name": "x/y"},
        "sender": {"login": "u"},
    }
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body, event="pull_request"))
    assert event.kind is EventKind.PR_SYNCHRONIZED


def test_parse_issue_comment_on_pr() -> None:
    # GitHub sends event=issue_comment for comments on both issues and PRs;
    # only the presence of `issue.pull_request` distinguishes them.
    payload = {
        "action": "created",
        "issue": {"number": 17, "pull_request": {"url": "..."}},
        "comment": {"body": "@openbot help"},
        "repository": {"full_name": "YiAgent/openbot"},
        "sender": {"login": "user"},
    }
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body, event="issue_comment"))

    assert event.kind is EventKind.ISSUE_COMMENT_CREATED
    assert event.pr_number == 17
    assert event.issue_number is None
    assert event.comment_body == "@openbot help"


def test_parse_issue_comment_on_plain_issue() -> None:
    payload = {
        "action": "created",
        "issue": {"number": 5},
        "comment": {"body": "@openbot what is this?"},
        "repository": {"full_name": "x/y"},
        "sender": {"login": "u"},
    }
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body, event="issue_comment"))

    assert event.kind is EventKind.ISSUE_COMMENT_CREATED
    assert event.issue_number == 5
    assert event.pr_number is None


def test_parse_issue_assigned() -> None:
    payload = {
        "action": "assigned",
        "issue": {"number": 1},
        "repository": {"full_name": "x/y"},
        "sender": {"login": "owner"},
    }
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body, event="issues"))
    assert event.kind is EventKind.ISSUE_ASSIGNED


def test_unknown_event_returns_unknown_kind() -> None:
    body = b'{"action":"created"}'
    event = _adapter().parse_event(body, _sign(body, event="star"))
    assert event.kind is EventKind.UNKNOWN
    assert not event.is_relevant


def test_parse_rejects_invalid_json() -> None:
    body = b"not json"
    with pytest.raises(SignatureError, match="not valid JSON"):
        _adapter().parse_event(body, _sign(body))
