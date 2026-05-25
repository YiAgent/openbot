"""Smoke tests — application boot + liveness.

These tests verify that the FastAPI app can be imported and respond
to lightweight probes.  They do NOT test business logic; they confirm
that the module graph is intact and Settings can be constructed.

Budget: all tests must complete in < 3 s total (smoke layer target).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.smoke
class TestAppBoot:
    async def test_app_imports_without_error(self, boot_env: None) -> None:
        """openbot.entrypoints.api.app must import and expose an ASGI callable."""
        from openbot.core.settings import get_settings

        get_settings.cache_clear()
        from openbot.entrypoints.api.app import app

        assert callable(app)

    async def test_health_returns_200(self, boot_env: None) -> None:
        """GET /health must return HTTP 200 with status=ok."""
        from openbot.core.settings import get_settings

        get_settings.cache_clear()
        from openbot.entrypoints.api.app import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_health_includes_version(self, boot_env: None) -> None:
        """GET /health must include a non-empty version string."""
        from openbot.core.settings import get_settings

        get_settings.cache_clear()
        from openbot.entrypoints.api.app import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")
        assert "version" in response.json()
        assert len(response.json()["version"]) > 0

    async def test_ready_returns_json(self, boot_env: None) -> None:
        """GET /ready must return JSON with a 'ready' key."""
        from openbot.core.settings import get_settings

        get_settings.cache_clear()
        from openbot.entrypoints.api.app import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/ready")
        body = response.json()
        assert "ready" in body
        assert isinstance(body["ready"], bool)

    async def test_ready_includes_components(self, boot_env: None) -> None:
        """GET /ready must include a 'components' dict."""
        from openbot.core.settings import get_settings

        get_settings.cache_clear()
        from openbot.entrypoints.api.app import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/ready")
        body = response.json()
        assert "components" in body
        assert isinstance(body["components"], dict)


@pytest.mark.smoke
class TestSettingsBoot:
    def test_settings_constructible_with_minimal_env(self, boot_env: None) -> None:
        """Settings() must not raise with only the minimum required env vars."""
        from openbot.core.settings import get_settings

        get_settings.cache_clear()
        settings = get_settings()
        # Settings constructible without raising; field has a default value
        assert settings is not None

    def test_settings_is_cached(self, boot_env: None) -> None:
        """get_settings() must return the same object on repeated calls."""
        from openbot.core.settings import get_settings

        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_ports_package_importable(self) -> None:
        """openbot.application.ports must be importable without any env vars."""
        import openbot.application.ports  # noqa: F401

        assert True
