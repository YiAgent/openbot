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

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.adapters.base import SignatureError
from openbot.infrastructure.adapters.github import GitHubAdapter
from openbot.infrastructure.adapters.github_auth import GitHubAppAuth, InstallationToken

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


# ───── sender.type → actor_type / is_from_bot ─────


def test_parse_extracts_user_actor_type() -> None:
    payload = _issue_opened_payload()
    payload["sender"] = {"login": "yiwang", "type": "User"}
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body))
    assert event.actor_type == "User"
    assert event.is_from_bot is False


def test_parse_extracts_bot_actor_type() -> None:
    payload = _issue_opened_payload()
    payload["sender"] = {"login": "dependabot[bot]", "type": "Bot"}
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body))
    assert event.actor_type == "Bot"
    assert event.is_from_bot is True


def test_parse_missing_actor_type_defaults_to_none() -> None:
    payload = _issue_opened_payload()
    # sender has login but no type field (defensive)
    payload["sender"] = {"login": "someone"}
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body))
    assert event.actor_type is None
    assert event.is_from_bot is False


# ───── clone_url / review_commit_id ingest (Task 1.8) ─────


def test_parse_pr_opened_extracts_clone_url() -> None:
    """PR webhooks carry the HTTPS clone URL — promote it to a typed field
    so the dispatcher's checkout resolver doesn't have to dig through `raw`.
    """
    payload = {
        "action": "opened",
        "pull_request": {"number": 17},
        "repository": {
            "full_name": "YiAgent/openbot",
            "clone_url": "https://github.com/YiAgent/openbot.git",
        },
        "sender": {"login": "contributor"},
    }
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body, event="pull_request"))
    assert event.clone_url == "https://github.com/YiAgent/openbot.git"


def test_parse_issue_opened_extracts_clone_url() -> None:
    payload = _issue_opened_payload() | {
        "repository": {
            "full_name": "YiAgent/openbot",
            "clone_url": "https://github.com/YiAgent/openbot.git",
        }
    }
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body))
    assert event.clone_url == "https://github.com/YiAgent/openbot.git"


def test_parse_missing_clone_url_is_none() -> None:
    """Adapter does not invent a URL — the resolver decides what to do."""
    body = json.dumps(_issue_opened_payload()).encode()  # no `clone_url`
    event = _adapter().parse_event(body, _sign(body))
    assert event.clone_url is None


def test_parse_pr_review_comment_extracts_commit_id() -> None:
    """Inline PR review comments carry the commit SHA they were left on —
    promote it so the review workflow can checkout the *exact* commit.
    """
    payload = {
        "action": "created",
        "pull_request": {"number": 17},
        "comment": {
            "body": "nit",
            "commit_id": "abc123def456" + "0" * 28,
        },
        "repository": {"full_name": "YiAgent/openbot"},
        "sender": {"login": "reviewer"},
    }
    body = json.dumps(payload).encode()
    event = _adapter().parse_event(body, _sign(body, event="pull_request_review_comment"))
    assert event.review_commit_id == "abc123def456" + "0" * 28


def test_parse_missing_review_commit_id_is_none() -> None:
    """Non-review events don't have a commit_id; field stays None."""
    body = json.dumps(_issue_opened_payload()).encode()
    event = _adapter().parse_event(body, _sign(body))
    assert event.review_commit_id is None


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


# ───── get_pr_diff ─────

_SAMPLE_DIFF = (
    "diff --git a/foo.py b/foo.py\n"
    "index 0000000..1111111 100644\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


async def test_get_pr_diff_returns_raw_diff_text(adapter_factory: Any) -> None:
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(200, text=_SAMPLE_DIFF), auth=_FakeAuth()
    )

    diff = await adapter.get_pr_diff(_event(issue_number=None, pr_number=7), 7)

    assert diff == _SAMPLE_DIFF
    req = captured[0]
    assert req.method == "GET"
    assert str(req.url) == "https://api.github.com/repos/YiAgent/openbot/pulls/7"
    assert req.headers["accept"] == "application/vnd.github.v3.diff"
    assert req.headers["authorization"] == f"token {_INSTALL_TOKEN}"


async def test_get_pr_diff_returns_empty_on_404(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(404, text="Not Found"), auth=_FakeAuth()
    )
    assert await adapter.get_pr_diff(_event(issue_number=None, pr_number=7), 7) == ""


async def test_get_pr_diff_raises_on_5xx(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(lambda req: httpx.Response(503, text="upstream"), auth=_FakeAuth())
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.get_pr_diff(_event(issue_number=None, pr_number=7), 7)


# ───── read_file ─────


def _contents_payload(text: str | bytes) -> dict[str, str]:
    """GitHub Contents API returns base64-encoded blobs."""
    import base64 as _b64

    raw = text.encode() if isinstance(text, str) else text
    return {"content": _b64.b64encode(raw).decode(), "encoding": "base64"}


async def test_read_file_returns_decoded_text(adapter_factory: Any) -> None:
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(200, json=_contents_payload("hello world\n")),
        auth=_FakeAuth(),
    )

    text = await adapter.read_file(_event(), "README.md")

    assert text == "hello world\n"
    assert str(captured[0].url).endswith("/contents/README.md")


async def test_read_file_returns_empty_on_404(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(lambda req: httpx.Response(404, text=""), auth=_FakeAuth())
    assert await adapter.read_file(_event(), "missing.py") == ""


async def test_read_file_returns_empty_on_binary(adapter_factory: Any) -> None:
    # Non-UTF-8 bytes — Contents API still returns base64, but decode fails.
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(200, json=_contents_payload(b"\x80\x81\x82")),
        auth=_FakeAuth(),
    )
    assert await adapter.read_file(_event(), "logo.png") == ""


# ───── grep_repo ─────


_GREP_RESPONSE = {
    "total_count": 2,
    "items": [
        {
            "path": "src/auth.py",
            "text_matches": [
                {"fragment": "def login(username):\n    raise NotImplementedError()"},
            ],
        },
        {
            "path": "src/db.py",
            "text_matches": [{"fragment": "return User.find(login=login)"}],
        },
    ],
}


async def test_grep_repo_returns_path_and_fragment(adapter_factory: Any) -> None:
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(200, json=_GREP_RESPONSE), auth=_FakeAuth()
    )

    matches = await adapter.grep_repo(_event(), pattern="login", path_glob="src")

    assert len(matches) == 2
    assert any(m.startswith("src/auth.py:") and "def login" in m for m in matches)
    assert any(m.startswith("src/db.py:") for m in matches)

    req = captured[0]
    assert "/search/code" in str(req.url)
    # GitHub Code Search query string carries the pattern + repo + path qualifiers.
    # The adapter URL-encodes the whole `q=` value, so decode before matching.
    from urllib.parse import unquote

    qs = unquote(str(req.url))
    assert "login" in qs
    assert f"repo:{_event().repo}" in qs
    assert "path:src" in qs
    assert req.headers["accept"].startswith("application/vnd.github.text-match")


async def test_grep_repo_omits_path_qualifier_when_glob_none(adapter_factory: Any) -> None:
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(200, json={"total_count": 0, "items": []}),
        auth=_FakeAuth(),
    )

    await adapter.grep_repo(_event(), pattern="login", path_glob=None)

    qs = str(captured[0].url).replace("+", " ")
    assert "path:" not in qs


async def test_grep_repo_caps_to_max_matches(adapter_factory: Any) -> None:
    items = [{"path": f"src/f{i}.py", "text_matches": [{"fragment": f"hit{i}"}]} for i in range(50)]
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(200, json={"total_count": 50, "items": items}),
        auth=_FakeAuth(),
    )

    matches = await adapter.grep_repo(_event(), pattern="hit", path_glob=None, max_matches=10)

    assert len(matches) == 10


async def test_grep_repo_returns_empty_on_422(adapter_factory: Any) -> None:
    # GitHub returns 422 when the search query is invalid or the repo isn't
    # indexed yet — must not raise; review must still post a reply.
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(422, json={"message": "Validation failed"}),
        auth=_FakeAuth(),
    )
    assert await adapter.grep_repo(_event(), pattern="x", path_glob=None) == []


async def test_grep_repo_skips_items_without_text_matches(adapter_factory: Any) -> None:
    # Defensive: GitHub may return items with no `text_matches` array if the
    # caller forgot the Accept header — our adapter sets it, but a wire-level
    # quirk shouldn't crash the agent.
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(200, json={"total_count": 1, "items": [{"path": "x.py"}]}),
        auth=_FakeAuth(),
    )
    assert await adapter.grep_repo(_event(), pattern="x", path_glob=None) == []


# ───── create_pr_review ─────


_REVIEW_RESPONSE = {"id": 9000, "state": "COMMENTED"}


async def test_create_pr_review_posts_to_pulls_reviews_endpoint(adapter_factory: Any) -> None:
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(200, json=_REVIEW_RESPONSE), auth=_FakeAuth()
    )

    result = await adapter.create_pr_review(
        _event(pr_number=77),
        pr_number=77,
        body="LGTM overall",
        event_type="APPROVE",
    )

    assert result == _REVIEW_RESPONSE
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.github.com/repos/YiAgent/openbot/pulls/77/reviews"
    sent = json.loads(req.content)
    assert sent["body"] == "LGTM overall"
    assert sent["event"] == "APPROVE"
    # No comments key means GitHub treats it as a body-only review — that's
    # exactly what we want for an APPROVE with no inline findings.
    assert sent.get("comments", []) == []


async def test_create_pr_review_sends_inline_comments(adapter_factory: Any) -> None:
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(200, json=_REVIEW_RESPONSE), auth=_FakeAuth()
    )
    inline = [
        {"path": "src/auth.py", "line": 42, "body": "**high** — bug here"},
        {"path": "src/db.py", "line": 10, "body": "**medium** — race"},
    ]

    await adapter.create_pr_review(
        _event(pr_number=12),
        pr_number=12,
        body="2 findings",
        event_type="COMMENT",
        comments=inline,
    )

    sent = json.loads(captured[0].content)
    assert sent["event"] == "COMMENT"
    assert sent["comments"] == inline


async def test_create_pr_review_raises_when_no_auth(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(lambda req: httpx.Response(500), auth=None)
    with pytest.raises(RuntimeError, match="write-back unavailable"):
        await adapter.create_pr_review(
            _event(pr_number=1), pr_number=1, body="x", event_type="COMMENT"
        )


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
    with caplog.at_level(logging.WARNING, logger="openbot.infrastructure.adapters.github"):
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
    with caplog.at_level(logging.WARNING, logger="openbot.infrastructure.adapters.github"):
        await adapter.reply(_event(), "hi")

    assert not [r for r in caplog.records if r.message == "github_rate_limit_low"]


# ───── lifecycle ─────


async def test_aclose_releases_owned_http_client() -> None:
    adapter = GitHubAdapter(webhook_secret=_SECRET, auth=_FakeAuth())  # type: ignore[arg-type]
    await adapter.aclose()
    # Calling it twice is safe even though the client is already closed.
    await adapter.aclose()


# ───── get_issue ─────


async def test_get_issue_batches_three_calls(adapter_factory: Any) -> None:
    """Issue body + comments + default-branch repo metadata fetched in one batch."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/issues/42") and not url.endswith("/comments"):
            return httpx.Response(
                200,
                json={
                    "title": "Bug: pagination off-by-one",
                    "body": "Page 2 returns rows 9-17 instead of 10-19.",
                },
            )
        if url.endswith("/issues/42/comments"):
            return httpx.Response(
                200,
                json=[
                    {"user": {"login": "alice"}, "body": "Repro on main."},
                    {"user": {"login": "bob"}, "body": "Looks like LIMIT/OFFSET swap."},
                ],
            )
        if url.endswith("/repos/YiAgent/openbot"):
            return httpx.Response(
                200,
                json={
                    "default_branch": "main",
                    "clone_url": "https://github.com/YiAgent/openbot.git",
                },
            )
        if url.endswith("/repos/YiAgent/openbot/git/ref/heads/main"):
            return httpx.Response(
                200,
                json={"object": {"sha": "deadbeefcafebabe1234567890abcdef12345678"}},
            )
        raise AssertionError(f"unexpected url: {url}")

    adapter, captured = adapter_factory(handler, auth=_FakeAuth())
    issue = await adapter.get_issue(_event(issue_number=42), 42)

    assert issue["title"] == "Bug: pagination off-by-one"
    assert issue["body"].startswith("Page 2")
    assert issue["comments"] == [
        {"author": "alice", "body": "Repro on main."},
        {"author": "bob", "body": "Looks like LIMIT/OFFSET swap."},
    ]
    assert issue["default_branch"] == "main"
    assert issue["clone_url"] == "https://github.com/YiAgent/openbot.git"
    assert issue["base_sha"] == "deadbeefcafebabe1234567890abcdef12345678"
    # 4 requests: issue, comments, repo metadata, default-branch ref
    assert len(captured) == 4


async def test_get_issue_raises_on_404(adapter_factory: Any) -> None:
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(404, json={"message": "Not Found"}),
        auth=_FakeAuth(),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.get_issue(_event(issue_number=999), 999)


# ───── get_default_branch_sha ─────


async def test_get_default_branch_sha_resolves_repo_default_branch(
    adapter_factory: Any,
) -> None:
    """Two-call dance: repo metadata to learn the default branch name,
    then git/ref/heads/{branch} for the commit SHA. The resolver feeds
    this into a CheckoutSpec when the event itself doesn't already
    carry a SHA (e.g. label flips, ad-hoc chat mentions)."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/repos/YiAgent/openbot"):
            return httpx.Response(
                200,
                json={
                    "default_branch": "main",
                    "clone_url": "https://github.com/YiAgent/openbot.git",
                },
            )
        if url.endswith("/repos/YiAgent/openbot/git/ref/heads/main"):
            return httpx.Response(
                200,
                json={"object": {"sha": "deadbeef1234567890abcdef1234567890abcdef"}},
            )
        raise AssertionError(f"unexpected url: {url}")

    adapter, captured = adapter_factory(handler, auth=_FakeAuth())
    sha = await adapter.get_default_branch_sha(_event(issue_number=None, pr_number=None))
    assert sha == "deadbeef1234567890abcdef1234567890abcdef"
    # Exactly two GETs — repo metadata + ref lookup. Caching across
    # events stays out of scope for v0.1 (event volume is low; tokens
    # are short-lived) so the call count is intentional.
    assert len(captured) == 2


async def test_get_default_branch_sha_follows_non_default_branch_name(
    adapter_factory: Any,
) -> None:
    """Repos with a custom default branch ('trunk', 'develop', etc.) still resolve."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/repos/YiAgent/openbot"):
            return httpx.Response(200, json={"default_branch": "trunk"})
        if url.endswith("/repos/YiAgent/openbot/git/ref/heads/trunk"):
            return httpx.Response(200, json={"object": {"sha": "cafebabe"}})
        raise AssertionError(f"unexpected url: {url}")

    adapter, _ = adapter_factory(handler, auth=_FakeAuth())
    sha = await adapter.get_default_branch_sha(_event())
    assert sha == "cafebabe"


async def test_get_default_branch_sha_defaults_to_main_when_metadata_missing(
    adapter_factory: Any,
) -> None:
    """If the repo endpoint omits ``default_branch`` (unexpected but
    possible on a permissions-stripped response), fall back to 'main'
    rather than raising — the caller treats 'no SHA' as a clean failure."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/repos/YiAgent/openbot"):
            return httpx.Response(200, json={})
        if url.endswith("/repos/YiAgent/openbot/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "abc123"}})
        raise AssertionError(f"unexpected url: {url}")

    adapter, _ = adapter_factory(handler, auth=_FakeAuth())
    sha = await adapter.get_default_branch_sha(_event())
    assert sha == "abc123"


async def test_get_default_branch_sha_raises_on_repo_404(
    adapter_factory: Any,
) -> None:
    """A 404 on the repo endpoint is a hard failure — the resolver
    can't pick a checkout without it. Propagate so the use case can
    short-circuit and post a tailored comment."""
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(404, json={"message": "Not Found"}),
        auth=_FakeAuth(),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.get_default_branch_sha(_event())


# ───── get_pull_request (used by checkout_resolver) ─────


async def test_get_pull_request_returns_full_pr_json(adapter_factory: Any) -> None:
    """GET /repos/{owner}/{repo}/pulls/{n} — used by the resolver to
    get head/base SHAs that the webhook payload doesn't carry (e.g.
    ``issue_comment`` events on a PR)."""

    def handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url).endswith("/repos/YiAgent/openbot/pulls/17")
        return httpx.Response(
            200,
            json={
                "number": 17,
                "head": {"sha": "a" * 40, "ref": "feature/x"},
                "base": {"sha": "b" * 40, "ref": "main"},
                "state": "open",
            },
        )

    adapter, _ = adapter_factory(handler, auth=_FakeAuth())
    pr = await adapter.get_pull_request(_event(pr_number=17), 17)
    assert pr["head"]["sha"] == "a" * 40
    assert pr["base"]["sha"] == "b" * 40


async def test_get_pull_request_raises_on_404(adapter_factory: Any) -> None:
    """Closed/deleted PRs surface as 404 — propagate so the resolver
    can convert it into a ``CheckoutResolutionError``."""
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(404, json={"message": "Not Found"}),
        auth=_FakeAuth(),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.get_pull_request(_event(pr_number=999), 999)


async def test_get_pull_request_rejects_non_dict_payload(adapter_factory: Any) -> None:
    """Defensive: GitHub always returns an object here, but if some
    proxy returns a list/string the resolver shouldn't silently
    propagate garbage."""
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(200, json=["unexpected", "shape"]),
        auth=_FakeAuth(),
    )
    with pytest.raises(ValueError, match="unexpected PR shape"):
        await adapter.get_pull_request(_event(pr_number=17), 17)


async def test_get_issue_handles_empty_body_and_no_comments(adapter_factory: Any) -> None:
    """Issue with `body: null` and zero comments still produces a valid dict."""

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/issues/7") and not url.endswith("/comments"):
            return httpx.Response(200, json={"title": "thin", "body": None})
        if url.endswith("/issues/7/comments"):
            return httpx.Response(200, json=[])
        if url.endswith("/repos/YiAgent/openbot"):
            return httpx.Response(
                200,
                json={
                    "default_branch": "trunk",
                    "clone_url": "https://github.com/YiAgent/openbot.git",
                },
            )
        if "/git/ref/heads/trunk" in url:
            return httpx.Response(200, json={"object": {"sha": "abc1234567890"}})
        raise AssertionError(f"unexpected url: {url}")

    adapter, _ = adapter_factory(handler, auth=_FakeAuth())
    issue = await adapter.get_issue(_event(issue_number=7), 7)

    assert issue["title"] == "thin"
    assert issue["body"] == ""  # null body normalized to empty string
    assert issue["comments"] == []
    assert issue["default_branch"] == "trunk"


# ───── create_branch ─────


async def test_create_branch_posts_git_refs(adapter_factory: Any) -> None:
    """POST /repos/{r}/git/refs with body {ref, sha}."""
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(
            201,
            json={"ref": "refs/heads/openbot/fix-issue-42-deadbee", "object": {"sha": "deadbee"}},
        ),
        auth=_FakeAuth(),
    )

    await adapter.create_branch(
        _event(),
        "openbot/fix-issue-42-deadbee",
        from_sha="deadbeefcafebabe1234567890abcdef12345678",
    )

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.github.com/repos/YiAgent/openbot/git/refs"
    body = json.loads(req.content)
    # GitHub expects the *full ref path*, not the short name.
    assert body == {
        "ref": "refs/heads/openbot/fix-issue-42-deadbee",
        "sha": "deadbeefcafebabe1234567890abcdef12345678",
    }


async def test_create_branch_raises_on_422_conflict(adapter_factory: Any) -> None:
    """422 = branch already exists. Caller surfaces a tailored comment."""
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(422, json={"message": "Reference already exists"}),
        auth=_FakeAuth(),
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await adapter.create_branch(_event(), "openbot/fix-issue-42-deadbee", from_sha="deadbee")
    assert exc_info.value.response.status_code == 422


# ───── open_pull_request ─────


async def test_open_pull_request_posts_pulls(adapter_factory: Any) -> None:
    """draft=False is implicit (GitHub default) — fix loop never opens drafts."""
    adapter, captured = adapter_factory(
        lambda req: httpx.Response(
            201,
            json={
                "number": 123,
                "html_url": "https://github.com/YiAgent/openbot/pull/123",
                "head": {"ref": "openbot/fix-issue-42-deadbee"},
            },
        ),
        auth=_FakeAuth(),
    )

    pr = await adapter.open_pull_request(
        _event(),
        title="Fix #42: pagination off-by-one",
        body="Closes #42\n\nSwapped LIMIT and OFFSET.",
        head="openbot/fix-issue-42-deadbee",
        base="main",
    )

    assert pr["html_url"] == "https://github.com/YiAgent/openbot/pull/123"
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.github.com/repos/YiAgent/openbot/pulls"
    body = json.loads(req.content)
    assert body == {
        "title": "Fix #42: pagination off-by-one",
        "body": "Closes #42\n\nSwapped LIMIT and OFFSET.",
        "head": "openbot/fix-issue-42-deadbee",
        "base": "main",
    }
    # draft must not be in the payload — never set explicitly, never True.
    assert "draft" not in body


async def test_open_pull_request_raises_on_422_no_diff(adapter_factory: Any) -> None:
    """No-commit branch → 422 "No commits between base and head".
    Use case surfaces this as the "tests passed but no changes" path."""
    adapter, _ = adapter_factory(
        lambda req: httpx.Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [{"message": "No commits between main and ..."}],
            },
        ),
        auth=_FakeAuth(),
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await adapter.open_pull_request(_event(), title="t", body="b", head="x", base="main")
    assert exc_info.value.response.status_code == 422


# ───── get_installation_token ─────


async def test_get_installation_token_returns_raw_string(adapter_factory: Any) -> None:
    """The application layer never sees InstallationToken — only the raw bearer."""
    auth = _FakeAuth()
    adapter, _ = adapter_factory(lambda req: httpx.Response(500), auth=auth)

    token = await adapter.get_installation_token(_event())

    assert token == _INSTALL_TOKEN
    assert auth.calls == [_INSTALL_ID]
