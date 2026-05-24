"""Settings — env-driven construction + cache invalidation."""

from __future__ import annotations

import pytest

from openbot.core.settings import Settings, get_settings


def test_settings_constructible_with_minimal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "0")
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", "x")
    get_settings.cache_clear()
    s = Settings()
    assert s.github_app_id == 0


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "1")
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", "x")
    get_settings.cache_clear()
    a = get_settings()
    b = get_settings()
    assert a is b


def test_invalid_app_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "not-an-int")
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", "x")
    with pytest.raises(ValidationError):
        Settings()
