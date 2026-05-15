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
