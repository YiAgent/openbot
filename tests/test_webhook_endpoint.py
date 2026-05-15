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
            "sender": {"login": "u"},
        }
    ).encode()
    response = client.post("/webhook/github", content=body, headers=_sign(body))

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["kind"] == "issue.opened"
    assert data["delivery_id"] == "deliv-1"
    assert data["relevant"] is True


def test_webhook_accepts_but_marks_irrelevant_unknown_event(client: TestClient) -> None:
    body = b'{"action":"created"}'
    response = client.post("/webhook/github", content=body, headers=_sign(body, event="star"))

    assert response.status_code == 202
    assert response.json()["relevant"] is False


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


def test_webhook_dedup_returns_duplicate_on_repeat_delivery(
    client: TestClient,
) -> None:
    """Repeat-delivery sanity: same X-GitHub-Delivery hitting the endpoint twice
    yields one 'accepted' + one 'duplicate'. Production Redis is unconfigured
    here, but lifespan still installs WebhookDedup with the no-redis fall-open
    path — so this test ALSO covers the fall-open does-not-dedup case, both
    requests get 'accepted'.
    """
    body = json.dumps(
        {
            "action": "opened",
            "issue": {"number": 1},
            "repository": {"full_name": "YiAgent/openbot"},
            "sender": {"login": "u", "type": "User"},
        }
    ).encode()
    headers = _sign(body)

    r1 = client.post("/webhook/github", content=body, headers=headers)
    r2 = client.post("/webhook/github", content=body, headers=headers)

    # Both 202 either way (fresh + fresh under fall-open; or fresh + duplicate
    # under real Redis). Status string differentiates.
    assert r1.status_code == 202
    assert r2.status_code == 202
    # Without OPENBOT_REDIS_URL set, dedup is open → both "accepted".
    # When Redis IS configured, second one is "duplicate".
    statuses = (r1.json()["status"], r2.json()["status"])
    assert statuses in (("accepted", "accepted"), ("accepted", "duplicate"))


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
