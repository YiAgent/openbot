"""Integration: POST /webhook/github via FastAPI TestClient."""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openbot.config import get_settings
from openbot.webapp import app

_SECRET = "test-secret"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", _SECRET)
    get_settings.cache_clear()
    # `with TestClient(...)` triggers the FastAPI lifespan — needed so
    # `app.state.github_adapter` is constructed from the patched env.
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _sign(body: bytes, event: str = "issues") -> dict[str, str]:
    return {
        "x-hub-signature-256": "sha256=" + hmac.new(_SECRET.encode(), body, sha256).hexdigest(),
        "x-github-event": event,
        "x-github-delivery": "deliv-1",
        "content-type": "application/json",
    }


def test_webhook_rejects_missing_signature(client: TestClient) -> None:
    response = client.post("/webhook/github", content=b"{}")
    assert response.status_code == 401


def test_webhook_rejects_tampered_body(client: TestClient) -> None:
    headers = _sign(b'{"action":"opened"}')
    response = client.post("/webhook/github", content=b'{"action":"closed"}', headers=headers)
    assert response.status_code == 401


def test_webhook_accepts_valid_signature(client: TestClient) -> None:
    body = json.dumps(
        {
            "action": "opened",
            "issue": {"number": 1},
            "repository": {"full_name": "YiAgent/openbot"},
            "sender": {"login": "u", "type": "User"},
            "installation": {"id": 12345},
        }
    ).encode()
    response = client.post("/webhook/github", content=body, headers=_sign(body))

    assert response.status_code == 202
    data = response.json()
    # Router matched → harness spec §3 M2: status="accepted" + feature + task_id.
    assert data["status"] == "accepted"
    assert data["kind"] == "issue.opened"
    assert data["delivery_id"] == "deliv-1"
    assert data["relevant"] is True
    assert data["feature"] == "triage"
    assert len(data["task_id"]) == 32  # sha256-hex truncated (spec §9.1)


def test_webhook_accepts_but_marks_irrelevant_unknown_event(client: TestClient) -> None:
    body = b'{"action":"created"}'
    response = client.post("/webhook/github", content=body, headers=_sign(body, event="star"))

    # Unknown event types: Router returns None → "ignored" status (vs the
    # pre-router "accepted but irrelevant" wording). Still 202 for GitHub.
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "ignored"
    assert payload["relevant"] is False


def test_webhook_503_when_secret_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Isolate from any ambient .env on disk — pydantic-settings looks for ./.env
    # relative to cwd. chdir to a clean tmp dir so only env vars matter.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENBOT_GITHUB_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
    with TestClient(app) as c:
        response = c.post("/webhook/github", content=b"{}")
    assert response.status_code == 503
    get_settings.cache_clear()


# ───── dedup integration ─────


def test_webhook_dedup_fallback_open_when_redis_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without OPENBOT_REDIS_URL, both repeats are processed (fall-open).

    Deterministic version of the prior "either-or" test: explicitly scrubs
    the env so the dedup is guaranteed to be in fall-open mode, and asserts
    both outcomes are "accepted".
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", _SECRET)
    monkeypatch.delenv("OPENBOT_REDIS_URL", raising=False)
    get_settings.cache_clear()

    body = json.dumps(
        {
            "action": "opened",
            "issue": {"number": 1},
            "repository": {"full_name": "YiAgent/openbot"},
            "sender": {"login": "u", "type": "User"},
            "installation": {"id": 99},
        }
    ).encode()
    headers = _sign(body) | {"x-github-delivery": "fallback-test-id"}

    try:
        with TestClient(app) as c:
            r1 = c.post("/webhook/github", content=body, headers=headers)
            r2 = c.post("/webhook/github", content=body, headers=headers)

        assert r1.status_code == 202 and r1.json()["status"] == "accepted"
        assert r2.status_code == 202 and r2.json()["status"] == "accepted"
    finally:
        get_settings.cache_clear()


async def test_webhook_enqueues_when_redis_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With Redis wired, the webhook XADDs to openbot:workflows instead of
    invoking the in-process BackgroundTask path. Slice D behavior."""
    import fakeredis.aioredis  # local import: dev-only dep

    from openbot.queue.payload import STREAM_NAME, deserialize_payload

    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", _SECRET)
    get_settings.cache_clear()

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("OPENBOT_REDIS_URL", "redis://fake")
    monkeypatch.setattr("openbot.webapp.make_client", lambda url: fake)

    body = json.dumps(
        {
            "action": "opened",
            "issue": {"number": 1},
            "repository": {"full_name": "YiAgent/openbot"},
            "sender": {"login": "u", "type": "User"},
            "installation": {"id": 555},
        }
    ).encode()
    headers = _sign(body) | {"x-github-delivery": "queue-test-id"}

    try:
        with TestClient(app) as c:
            response = c.post("/webhook/github", content=body, headers=headers)

        assert response.status_code == 202
        payload = response.json()
        assert payload["status"] == "accepted"
        # Slice D contract: the response includes the Redis Stream entry id.
        assert "entry_id" in payload
        assert "-" in payload["entry_id"]

        # And the payload that was XADD'd round-trips back to the same
        # UnifiedEvent shape — proves the webhook → queue serialization
        # path is intact end-to-end.
        entries = await fake.xrange(STREAM_NAME, count=10)
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        restored = deserialize_payload(fields["json"])
        assert restored is not None
        assert restored.delivery_id == "queue-test-id"
        assert restored.feature == "triage"
        assert restored.repo == "YiAgent/openbot"
    finally:
        get_settings.cache_clear()


def test_webhook_dedup_drops_workflow_when_redis_marks_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a real Redis (fakeredis here), the second delivery short-circuits
    BEFORE the workflow dispatch — assert status=duplicate, no error."""
    import fakeredis.aioredis  # local import: dev-only dep

    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", _SECRET)
    get_settings.cache_clear()

    # Patch make_client so the lifespan wires fakeredis instead of real redis.
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("OPENBOT_REDIS_URL", "redis://fake")  # any truthy value
    monkeypatch.setattr("openbot.webapp.make_client", lambda url: fake)

    try:
        body = json.dumps(
            {
                "action": "opened",
                "issue": {"number": 42},
                "repository": {"full_name": "YiAgent/openbot"},
                "sender": {"login": "u", "type": "User"},
                "installation": {"id": 77},
            }
        ).encode()
        headers = _sign(body) | {"x-github-delivery": "dup-test-id"}

        with TestClient(app) as c:
            r1 = c.post("/webhook/github", content=body, headers=headers)
            r2 = c.post("/webhook/github", content=body, headers=headers)

        assert r1.status_code == 202 and r1.json()["status"] == "accepted"
        assert r2.status_code == 202 and r2.json()["status"] == "duplicate"
        assert r2.json()["delivery_id"] == "dup-test-id"
    finally:
        get_settings.cache_clear()
