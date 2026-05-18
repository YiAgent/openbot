"""Settings: empty-string env vars coerce to None for optional non-string fields.

Heroku / Doppler-synced env stores commonly leave keys present-but-empty (e.g.
``OPENBOT_GITHUB_APP_ID=""`` when the operator hasn't filled it in yet). Default
pydantic coercion rejects ``""`` for ``int | None`` / ``Path | None`` even though
the documented intent — for receive-only deploys — is "leave it unset".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from openbot.core.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _reset_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_blank_app_id_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "")
    assert Settings().github_app_id is None


def test_whitespace_app_id_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "   ")
    assert Settings().github_app_id is None


def test_real_app_id_still_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "42")
    assert Settings().github_app_id == 42


def test_blank_private_key_path_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_GITHUB_APP_PRIVATE_KEY_PATH", "")
    assert Settings().github_app_private_key_path is None


def test_blank_redis_and_postgres_urls_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_REDIS_URL", "")
    monkeypatch.setenv("OPENBOT_POSTGRES_URL", "  ")
    s = Settings()
    assert s.redis_url is None
    assert s.postgres_url is None
