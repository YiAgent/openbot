"""POST /webhook/github e2e — every supported GitHub event kind.

Covers what ``tests/test_webhook_endpoint.py`` only sketches (issues.opened
plus dedup): a realistic GitHub-shape payload per event kind, fed through
the *real* FastAPI app via ``TestClient``, asserting the response body's
``status`` / ``feature`` / ``task_id`` / ``relevant`` fields match what the
router would produce.

Each test goes through:

    raw HTTP → HMAC verify (GitHubAdapter) → parse_event (GitHubAdapter)
            → dedup (no-op, Redis unset) → dispatch_for (router)
            → response body

The chain runs inside an in-process FastAPI app (lifespan + state machinery
real), but we *deliberately* leave Redis unset so:

  1. dedup falls open (each request is processed; no shared state pollutes
     tests),
  2. workflow dispatch goes through ``BackgroundTasks`` and exits cleanly
     after the test returns — no extra reaper threads.

The fixtures in ``_github_payloads`` produce realistic bodies + signed
headers; tests stay short and read like a behaviour spec.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from openbot.application.router import derive_task_id
from openbot.core.settings import get_settings
from openbot.domain.events import UnifiedEvent
from openbot.domain.workflows import Feature
from openbot.entrypoints.api.app import app
from tests.e2e._github_payloads import (
    WEBHOOK_SECRET,
    issue_assigned_to_bot,
    issue_assigned_to_human,
    issue_comment_on_issue_chat,
    issue_comment_on_pr_chat,
    issue_comment_unrelated,
    issue_opened,
    issue_opened_by_bot,
    malformed_json,
    ping,
    pr_opened,
    pr_review_comment_chat,
    pr_synchronize,
)

_CHANNEL = "github"
_REPO = "acme-corp/widgets"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """FastAPI TestClient with the webhook secret wired, no Redis.

    Scrubs ``OPENBOT_REDIS_URL`` so dedup falls open — each test owns its
    own delivery_id anyway, but without this an ambient ``.env`` on the
    dev machine could turn the second request in a test into a duplicate.
    """
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.delenv("OPENBOT_REDIS_URL", raising=False)
    monkeypatch.delenv("OPENBOT_POSTGRES_URL", raising=False)
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _expected_task_id(*, delivery_id: str) -> str:
    """Reuse production derivation so the test stays in sync with router.py."""
    # We only need the three fields derive_task_id reads — channel, repo,
    # delivery_id. Building a UnifiedEvent stub avoids hardcoding the
    # sha256 truncation rule in two places.
    return derive_task_id(
        UnifiedEvent(
            channel=_CHANNEL,
            delivery_id=delivery_id,
            kind=None,  # type: ignore[arg-type]  # derive_task_id doesn't read kind
            repo=_REPO,
            actor="x",
        )
    )


def _post(client: TestClient, headers: dict[str, str], body: bytes):
    return client.post("/webhook/github", content=body, headers=headers)


# ───────────────────────── issues ─────────────────────────


def test_issue_opened_dispatches_triage(client: TestClient) -> None:
    headers, body = issue_opened(delivery_id="e2e-iss-1", number=7)
    response = _post(client, headers, body)

    assert response.status_code == 202
    data = response.json()
    assert data == {
        "status": "accepted",
        "delivery_id": "e2e-iss-1",
        "kind": "issue.opened",
        "feature": Feature.TRIAGE.value,
        "task_id": _expected_task_id(delivery_id="e2e-iss-1"),
        "relevant": True,
        "check_run_id": None,
    }


def test_issue_assigned_to_bot_dispatches_fix(client: TestClient) -> None:
    headers, body = issue_assigned_to_bot(delivery_id="e2e-fix-1", number=11)
    response = _post(client, headers, body)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["kind"] == "issue.assigned"
    assert data["feature"] == Feature.FIX.value
    assert data["relevant"] is True


def test_issue_assigned_to_human_is_ignored(client: TestClient) -> None:
    """Router gates fix on assignee.type=="Bot"; a human assignment is a no-op.

    We still 202 (GitHub got delivery), but ``status=="ignored"`` and there
    is no ``feature`` / ``task_id`` — the response shape is the irrelevant
    one (see webapp.github_webhook).
    """
    headers, body = issue_assigned_to_human(delivery_id="e2e-fix-skip-1")
    response = _post(client, headers, body)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "ignored"
    # ``kind`` is still set — the *parser* recognised the event; the
    # *router* declined to dispatch a workflow.
    assert data["kind"] == "issue.assigned"
    assert data["relevant"] is True
    assert "feature" not in data
    assert "task_id" not in data


# ───────────────────────── pull_request ─────────────────────────


def test_pr_opened_dispatches_review(client: TestClient) -> None:
    headers, body = pr_opened(delivery_id="e2e-pr-1", number=23)
    response = _post(client, headers, body)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["kind"] == "pull_request.opened"
    assert data["feature"] == Feature.REVIEW.value
    assert data["task_id"] == _expected_task_id(delivery_id="e2e-pr-1")


def test_pr_synchronize_dispatches_review(client: TestClient) -> None:
    """`pull_request.synchronize` = follow-up push to an open PR; rerun review."""
    headers, body = pr_synchronize(delivery_id="e2e-pr-sync-1", number=23)
    response = _post(client, headers, body)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["kind"] == "pull_request.synchronize"
    assert data["feature"] == Feature.REVIEW.value


# ───────────────────────── comments → chat ─────────────────────────


def test_issue_comment_with_at_openbot_dispatches_chat(client: TestClient) -> None:
    headers, body = issue_comment_on_issue_chat(
        delivery_id="e2e-chat-1",
        issue_number=5,
        body="@openbot what does this code do?",
    )
    response = _post(client, headers, body)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["kind"] == "issue_comment.created"
    assert data["feature"] == Feature.CHAT.value


def test_issue_comment_on_pr_with_at_openbot_dispatches_chat(client: TestClient) -> None:
    """`issue_comment.created` on a PR — GitHub uses the issue_comment event
    even for PR conversation comments; presence of ``issue.pull_request``
    is the only signal."""
    headers, body = issue_comment_on_pr_chat(delivery_id="e2e-chat-pr-1", pr_number=23)
    response = _post(client, headers, body)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["kind"] == "issue_comment.created"
    assert data["feature"] == Feature.CHAT.value


def test_issue_comment_without_at_openbot_is_ignored(client: TestClient) -> None:
    headers, body = issue_comment_unrelated(delivery_id="e2e-chat-skip-1")
    response = _post(client, headers, body)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "ignored"
    assert data["kind"] == "issue_comment.created"
    assert "feature" not in data


def test_pr_review_comment_with_at_openbot_dispatches_chat(client: TestClient) -> None:
    """Code-line review comment (distinct webhook event from issue_comment).

    Mentions the bot inline on a code line, e.g. during a review:
        @openbot explain this hunk
    """
    headers, body = pr_review_comment_chat(delivery_id="e2e-review-chat-1", pr_number=23)
    response = _post(client, headers, body)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["kind"] == "pull_request_review_comment.created"
    assert data["feature"] == Feature.CHAT.value


# ───────────────────────── ping / bot author / malformed ─────────────────────────


def test_ping_event_is_accepted_but_ignored(client: TestClient) -> None:
    """GitHub sends ``ping`` once on App install. No ``action`` field, no
    workflow to run — we 202 with ``status=="ignored"``."""
    headers, body = ping(delivery_id="e2e-ping-1")
    response = _post(client, headers, body)

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "ignored"
    # parse_event maps ping → UNKNOWN kind because ("ping", "") isn't in
    # _EVENT_TABLE. That makes is_relevant=False.
    assert data["kind"] == "unknown"
    assert data["relevant"] is False


def test_issue_opened_by_bot_is_ignored_echo_guard(client: TestClient) -> None:
    """Bot-authored events MUST be dropped — prevents echo loops where a
    bot's own comment fires another webhook that re-triggers a workflow."""
    headers, body = issue_opened_by_bot(delivery_id="e2e-bot-iss-1")
    response = _post(client, headers, body)

    assert response.status_code == 202
    data = response.json()
    # parse_event still sees this as ISSUE_OPENED (relevant=True), but
    # the router drops it via the ``event.is_from_bot`` short-circuit.
    assert data["status"] == "ignored"
    assert data["kind"] == "issue.opened"
    assert data["relevant"] is True
    assert "feature" not in data


def test_malformed_json_with_valid_hmac_is_401(client: TestClient) -> None:
    """Valid HMAC over invalid JSON is treated as an attack signal — 401.

    Rationale lives in github.py::parse_event: a passer-by who can sign
    arbitrary bytes is far more concerning than one who sends garbage,
    so we collapse the "bad JSON" failure into the same 401 lane as a
    bad signature.
    """
    headers, body = malformed_json(delivery_id="e2e-bad-json-1")
    response = _post(client, headers, body)
    assert response.status_code == 401


def test_invalid_signature_is_401(client: TestClient) -> None:
    """Sanity: tamper one byte after signing → HMAC mismatch → 401.

    Already covered in tests/test_webhook_endpoint.py, but the e2e suite
    should be self-contained — anyone reading just this file gets the
    full webhook contract.
    """
    headers, body = issue_opened(delivery_id="e2e-sig-bad-1")
    tampered = body.replace(b'"opened"', b'"closed"')
    response = _post(client, headers, tampered)
    assert response.status_code == 401


# ───────────────────────── headers spot-check ─────────────────────────


def test_response_headers_include_2xx_and_json(client: TestClient) -> None:
    """Defensive: response is JSON (not HTML error page), status is 2xx."""
    headers, body = issue_opened(delivery_id="e2e-hdr-1")
    response = _post(client, headers, body)
    assert response.status_code == 202
    assert response.headers["content-type"].startswith("application/json")
