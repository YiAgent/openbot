"""Integration: GitHub Check Run lifecycle (webhook -> worker)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from openbot.core.settings import get_settings
from openbot.entrypoints.api.app import app
from openbot.infrastructure.queue.enqueue import RedisStreamQueue


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "123456")
    # Mocking a PEM so _build_auth doesn't return None.
    monkeypatch.setenv("OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM", "-----BEGIN RSA PRIVATE KEY-----")
    get_settings.cache_clear()
    with TestClient(app) as c:
        # Inject a fakeredis instance so ingest_webhook can enqueue.
        fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        c.app.state.redis = fake_redis
        c.app.state.queue = RedisStreamQueue(fake_redis)
        yield c
    get_settings.cache_clear()


def _sign(body: bytes, secret: str = "test-secret") -> dict[str, str]:
    import hmac
    from hashlib import sha256

    return {
        "x-hub-signature-256": "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest(),
        "x-github-event": "pull_request",
        "x-github-delivery": "deliv-pr-1",
        "content-type": "application/json",
    }


def test_webhook_creates_check_run_for_pr_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # We need to mock create_check_run on the adapter instance in app.state
    adapter = client.app.state.github_adapter
    assert adapter is not None
    adapter.create_check_run = AsyncMock(return_value={"id": 999})

    body = json.dumps(
        {
            "action": "opened",
            "pull_request": {"number": 101, "head": {"sha": "headsha123"}},
            "repository": {"full_name": "YiAgent/openbot"},
            "sender": {"login": "u", "type": "User"},
            "installation": {"id": 12345},
        }
    ).encode()

    response = client.post("/webhook/github", content=body, headers=_sign(body))

    assert response.status_code == 202
    data = response.json()
    assert data["check_run_id"] == 999

    adapter.create_check_run.assert_called_once()
    _args, kwargs = adapter.create_check_run.call_args
    assert kwargs["head_sha"] == "headsha123"
    assert kwargs["name"] == "OpenBot Analysis"


@pytest.mark.asyncio
async def test_execute_handler_updates_check_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from openbot.application.dispatcher import execute_handler
    from openbot.application.router import Dispatch
    from openbot.domain.events import EventKind, UnifiedEvent
    from openbot.domain.workflows import Feature

    adapter = AsyncMock()
    event = UnifiedEvent(
        channel="github",
        delivery_id="d1",
        kind=EventKind.PR_OPENED,
        repo="r",
        actor="a",
        pr_number=101,
        raw={"pull_request": {"head": {"sha": "s1"}}},
    )

    async def fake_handler(ctx: object) -> None:
        pass

    dispatch = Dispatch(feature=Feature.REVIEW, task_id="t1", handler=fake_handler)

    await execute_handler(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        check_run_id=888,
    )

    # Verify update_check_run was called with success.
    # Message matches execute_handler's actual summary format.
    adapter.update_check_run.assert_called_with(
        event,
        888,
        status="completed",
        conclusion="success",
        output={
            "title": "Analysis Complete",
            "summary": "Workflow `review` finished.",
        },
    )


@pytest.mark.asyncio
async def test_execute_handler_updates_check_run_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbot.application.dispatcher import execute_handler
    from openbot.application.router import Dispatch
    from openbot.domain.events import EventKind, UnifiedEvent
    from openbot.domain.workflows import Feature

    adapter = AsyncMock()
    event = UnifiedEvent(
        channel="github",
        delivery_id="d1",
        kind=EventKind.PR_OPENED,
        repo="r",
        actor="a",
        pr_number=101,
        raw={"pull_request": {"head": {"sha": "s1"}}},
    )

    async def crashing_handler(ctx: object) -> None:
        raise RuntimeError("boom")

    dispatch = Dispatch(feature=Feature.REVIEW, task_id="t1", handler=crashing_handler)

    await execute_handler(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config=AsyncMock(),
        session_factory=None,
        redis=None,
        check_run_id=777,
    )

    # Verify update_check_run was called with failure.
    # Message matches execute_handler's actual summary format.
    adapter.update_check_run.assert_called_with(
        event,
        777,
        status="completed",
        conclusion="failure",
        output={
            "title": "Handler Crash",
            "summary": "Handler `review` raised unexpectedly.",
        },
    )
