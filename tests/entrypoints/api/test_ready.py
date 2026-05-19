from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openbot.core.settings import get_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_ready_returns_503_when_required_config_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENBOT_GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("OPENBOT_REDIS_URL", raising=False)
    monkeypatch.delenv("OPENBOT_POSTGRES_URL", raising=False)

    from openbot.entrypoints.api.app import app

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["components"]["webhook"] == "missing_config"
    assert body["components"]["redis"] == "missing_config"
    assert body["components"]["postgres"] == "missing_config"


def test_ready_returns_200_when_all_checks_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("OPENBOT_REDIS_URL", "redis://example")
    monkeypatch.setenv("OPENBOT_POSTGRES_URL", "postgresql+asyncpg://example")

    import openbot.entrypoints.api.app as app_mod
    import openbot.entrypoints.api.routes.health as mod
    from openbot.entrypoints.api.app import app

    async def _redis_ok(_redis) -> bool:
        return True

    async def _db_ok(_engine) -> bool:
        return True

    async def _no_schema(_engine) -> None:
        return None

    class _Engine:
        async def dispose(self) -> None:
            return None

    fake_engine = _Engine()

    monkeypatch.setattr(mod, "_check_redis_ready", _redis_ok)
    monkeypatch.setattr(mod, "_check_db_ready", _db_ok)
    monkeypatch.setattr(app_mod, "make_engine", lambda *args, **kwargs: fake_engine)
    monkeypatch.setattr(app_mod, "create_schema", _no_schema)
    monkeypatch.setattr(app_mod, "make_session_factory", lambda _engine: None)
    app.state.redis = object()
    app.state.db_engine = fake_engine

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["components"]["webhook"] == "ok"
    assert body["components"]["redis"] == "ok"
    assert body["components"]["postgres"] == "ok"
