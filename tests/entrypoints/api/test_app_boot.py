"""Boot smoke — FastAPI app composes and exposes /health + /webhook/github."""

from __future__ import annotations

import logging

import pytest


def test_app_routes_present() -> None:
    """App module imports cleanly and registers expected routes.

    The import is deferred (inside the function body) so that module-level
    side effects — prometheus instrumentator, router inclusion — run in a
    controlled scope and don't bleed into other test modules.
    """
    from openbot.entrypoints.api.app import app

    routes = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/health" in routes
    assert "/ready" in routes
    assert "/webhook/github" in routes


def test_health_response_gets_generated_request_id() -> None:
    from fastapi.testclient import TestClient

    from openbot.entrypoints.api.app import app

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    request_id = response.headers.get("x-request-id")
    assert request_id is not None
    assert request_id != ""


def test_health_rejects_untrusted_host() -> None:
    from fastapi.testclient import TestClient

    from openbot.entrypoints.api.app import app

    with TestClient(app, base_url="http://evil.example") as client:
        response = client.get("/health")

    assert response.status_code == 400


def test_health_allows_heroku_host() -> None:
    from fastapi.testclient import TestClient

    from openbot.entrypoints.api.app import app

    with TestClient(app, base_url="https://openbot-ac02d94253df.herokuapp.com") as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_health_request_is_logged_with_request_id(caplog: pytest.LogCaptureFixture) -> None:
    from fastapi.testclient import TestClient

    from openbot.entrypoints.api.app import app

    with (
        caplog.at_level(logging.INFO, logger="openbot.entrypoints.api.app"),
        TestClient(app) as client,
    ):
        response = client.get("/health", headers={"x-request-id": "req-health-1"})

    assert response.status_code == 200
    record = next(
        r
        for r in caplog.records
        if r.name == "openbot.entrypoints.api.app" and r.msg == "http_request_completed"
    )
    assert record.method == "GET"
    assert record.path == "/health"
    assert record.status_code == 200
    assert record.request_id == "req-health-1"


@pytest.mark.asyncio
async def test_lifespan_attaches_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifespan populates app.state with all expected collaborators.

    Note: ``httpx.ASGITransport`` does NOT send the ASGI ``type=lifespan``
    scope, so we invoke the lifespan context manager directly.  The test
    passes without real Redis/Postgres — missing envvars make lifespan skip
    those backends and set the corresponding attrs to None.  We only verify
    the attrs EXIST (not their values), which is enough to prove the wiring
    is complete.
    """
    from openbot.core.settings import get_settings
    from openbot.entrypoints.api.app import app, lifespan

    # Clear settings cache so monkeypatched env takes effect.
    monkeypatch.delenv("OPENBOT_REDIS_URL", raising=False)
    monkeypatch.delenv("OPENBOT_DATABASE_URL", raising=False)
    monkeypatch.delenv("OPENBOT_GITHUB_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()

    try:
        async with lifespan(app):
            # Verify all expected state attrs were attached by lifespan
            expected_attrs = {
                "redis",
                "cancellation",
                "resource_lock",
                "dedup",
                "queue",
                "rate_limiter",
                "config_loader",
                "db_engine",
                "db_session_factory",
                "runs_repo",
                "audit",
                "github_auth",
                "github_adapter",
            }
            missing = {a for a in expected_attrs if not hasattr(app.state, a)}
            assert not missing, f"app.state missing after lifespan: {missing}"
    finally:
        # Always restore settings cache after test
        get_settings.cache_clear()
