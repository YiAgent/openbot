"""GitHubAdapter — signature verification, payload parsing, and write-back.

Covers PRD §4.8 (HMAC trust boundary), §5.1 (UnifiedEvent mapping), and
§13 #11 (ChannelAdapter write methods reply / add_label / remove_label /
get_actor_role).

Write-back uses `httpx.MockTransport` so no real network is touched.
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import httpx
import pytest

from openbot.adapters.base import SignatureError
from openbot.adapters.github import GitHubAdapter
from openbot.adapters.github_auth import GitHubAppAuth, InstallationToken
from openbot.events import EventKind, UnifiedEvent

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


def test_parse_extracts_installation_id() -> None:
    payload = _issue_opened_payload() | {"installation": {"id": 12345}}
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body))
    assert event.installation_id == 12345


def test_parse_missing_installation_is_none() -> None:
    body = json.dumps(_issue_opened_payload()).encode()  # no `installation`
    event = _adapter().parse_event(body, _sign(body))
    assert event.installation_id is None


# ─────────────────────────────────────────────────────────────────────────
# Write-back: reply / add_label / remove_label / get_actor_role
# All requests go through httpx.MockTransport — no real network.
# ─────────────────────────────────────────────────────────────────────────


_INSTALL_TOKEN = "ghs_install_token_xyz"
_INSTALL_ID = 9_999_999


class _FakeAuth:
    """Stand-in for GitHubAppAuth.installation_token — avoids JWT signing in tests."""

    def __init__(self, token: str = _INSTALL_TOKEN) -> None:
        self.calls: list[int] = []
        self._token = token

    async def installation_token(self, installation_id: int) -> InstallationToken:
        self.calls.append(installation_id)
        return InstallationToken(
            token=self._token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            installation_id=installation_id,
        )

    async def aclose(self) -> None:  # parity with GitHubAppAuth
        return None


def _event(
    *,
    kind: EventKind = EventKind.ISSUE_OPENED,
    issue_number: int | None = 42,
    pr_number: int | None = None,
    installation_id: int | None = _INSTALL_ID,
    repo: str = "YiAgent/openbot",
) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d1",
        kind=kind,
        repo=repo,
        actor="someone",
        issue_number=issue_number,
        pr_number=pr_number,
        installation_id=installation_id,
    )


@pytest.fixture
async def adapter_factory() -> Any:
    """Builds GitHubAdapters wired to httpx.MockTransport and auto-closes them.

    Each call returns `(adapter, captured_requests)`. Teardown drains every
    AsyncClient to keep ResourceWarning out of pytest's strict filter.
    """
    adapters: list[GitHubAdapter] = []

    def make(
        handler: Any,
        *,
        auth: GitHubAppAuth | _FakeAuth | None = None,
    ) -> tuple[GitHubAdapter, list[httpx.Request]]:
        captured: list[httpx.Request] = []

        def capturing(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return handler(request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(capturing))
        adapter = GitHubAdapter(
            webhook_secret=_SECRET,
            auth=auth,  # type: ignore[arg-type]
            http=http,
        )
        adapters.append(adapter)
        return adapter, captured

    yield make
    for a in adapters:
        await a.aclose()


# ───── reply ─────


async def test_reply_posts_to_issue_comments_endpoint(adapter_factory: Any) -> None:
    auth = _FakeAuth()
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(201, json={"id": 1001, "body": "hi"}), auth=auth
    )

    result = await adapter.reply(_event(issue_number=42), "hi")

    assert result == {"id": 1001, "body": "hi"}
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.github.com/repos/YiAgent/openbot/issues/42/comments"
    assert json.loads(req.content) == {"body": "hi"}
    # Token never appears in any log surface; assert it's in the header we sent.
    assert req.headers["authorization"] == f"token {_INSTALL_TOKEN}"
    assert req.headers["x-github-api-version"] == "2022-11-28"
    assert req.headers["user-agent"].startswith("OpenBot/")
    assert auth.calls == [_INSTALL_ID]


async def test_reply_uses_pr_number_when_issue_number_absent(adapter_factory: Any) -> None:
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(201, json={"id": 2}), auth=_FakeAuth()
    )
    await adapter.reply(_event(issue_number=None, pr_number=17), "x")
    assert "/issues/17/comments" in str(captured[0].url)


async def test_reply_raises_when_adapter_constructed_without_auth(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(lambda req: httpx.Response(500), auth=None)
    with pytest.raises(RuntimeError, match="write-back unavailable"):
        await adapter.reply(_event(), "hi")


async def test_reply_raises_when_installation_id_missing(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(lambda req: httpx.Response(500), auth=_FakeAuth())
    with pytest.raises(ValueError, match="installation_id"):
        await adapter.reply(_event(installation_id=None), "hi")


async def test_reply_raises_when_no_issue_or_pr_number(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(lambda req: httpx.Response(500), auth=_FakeAuth())
    with pytest.raises(ValueError, match="issue/PR number"):
        await adapter.reply(_event(issue_number=None, pr_number=None), "hi")


async def test_reply_propagates_4xx(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(404, json={"message": "Not Found"}), auth=_FakeAuth()
    )
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.reply(_event(), "hi")


# ───── add_label ─────


async def test_add_label_posts_labels_array(adapter_factory: Any) -> None:
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(200, json=[{"name": "bug"}, {"name": "priority/P1"}]),
        auth=_FakeAuth(),
    )

    result = await adapter.add_label(_event(), "bug", "priority/P1")

    assert {label["name"] for label in result} == {"bug", "priority/P1"}
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url).endswith("/issues/42/labels")
    assert json.loads(req.content) == {"labels": ["bug", "priority/P1"]}


async def test_add_label_rejects_no_labels(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(lambda req: httpx.Response(200), auth=_FakeAuth())
    with pytest.raises(ValueError, match="at least one label"):
        await adapter.add_label(_event())


# ───── remove_label ─────


async def test_remove_label_deletes_and_url_encodes(adapter_factory: Any) -> None:
    adapter, captured = adapter_factory(lambda req: httpx.Response(200, json=[]), auth=_FakeAuth())

    # Slashes in label names must be percent-encoded (PRD uses `priority/P1`).
    await adapter.remove_label(_event(), "priority/P1")

    req = captured[0]
    assert req.method == "DELETE"
    assert str(req.url).endswith("/issues/42/labels/priority%2FP1")


async def test_remove_label_is_idempotent_on_404(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(404, json={"message": "Label does not exist"}),
        auth=_FakeAuth(),
    )
    # Should not raise — removing an absent label is a no-op.
    await adapter.remove_label(_event(), "missing-label")


async def test_remove_label_propagates_5xx(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(503, json={"message": "down"}), auth=_FakeAuth()
    )
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.remove_label(_event(), "any")


# ───── get_actor_role ─────


@pytest.mark.parametrize(
    "permission",
    ["admin", "maintain", "write", "triage", "read", "none"],
)
async def test_get_actor_role_returns_permission(adapter_factory: Any, permission: str) -> None:
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(200, json={"permission": permission, "user": {}}),
        auth=_FakeAuth(),
    )

    role = await adapter.get_actor_role(_event())

    assert role == permission
    req = captured[0]
    assert req.method == "GET"
    assert str(req.url).endswith("/collaborators/someone/permission")


async def test_get_actor_role_defaults_to_none_when_missing(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(lambda req: httpx.Response(200, json={}), auth=_FakeAuth())
    assert await adapter.get_actor_role(_event()) == "none"


# ───── rate-limit warning ─────


async def test_low_rate_limit_emits_warning(
    adapter_factory: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"id": 1},
            headers={"x-ratelimit-remaining": "5"},
        )

    adapter, _ = adapter_factory(handler, auth=_FakeAuth())
    with caplog.at_level(logging.WARNING, logger="openbot.adapters.github"):
        await adapter.reply(_event(), "hi")

    matching = [r for r in caplog.records if r.message == "github_rate_limit_low"]
    assert len(matching) == 1
    assert matching[0].remaining == 5  # type: ignore[attr-defined]


async def test_normal_rate_limit_silent(
    adapter_factory: Any, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 1}, headers={"x-ratelimit-remaining": "4500"})

    adapter, _ = adapter_factory(handler, auth=_FakeAuth())
    with caplog.at_level(logging.WARNING, logger="openbot.adapters.github"):
        await adapter.reply(_event(), "hi")

    assert not [r for r in caplog.records if r.message == "github_rate_limit_low"]


# ───── lifecycle ─────


async def test_aclose_releases_owned_http_client() -> None:
    adapter = GitHubAdapter(webhook_secret=_SECRET, auth=_FakeAuth())  # type: ignore[arg-type]
    await adapter.aclose()
    # Calling it twice is safe even though the client is already closed.
    await adapter.aclose()
