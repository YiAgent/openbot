"""Tests for Settings.anthropic_api_base (CLAUDE_SWITCH_GLM_BASE_URL).

Covers:
- Default is None when env var absent
- Field is populated when CLAUDE_SWITCH_GLM_BASE_URL is set
- Blank value coerces to None
- SSRF validator rejects http:// and private-range IPs
- SSRF validator accepts valid https:// public URL
"""

from __future__ import annotations

import pytest

from openbot.core.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:  # type: ignore[return]
    """Ensure Settings cache is cleared around each test."""
    get_settings.cache_clear()
    yield  # type: ignore[misc]
    get_settings.cache_clear()


@pytest.fixture()
def _no_glm_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Remove CLAUDE_SWITCH_GLM_BASE_URL from environment and .env."""
    monkeypatch.delenv("CLAUDE_SWITCH_GLM_BASE_URL", raising=False)


def test_anthropic_api_base_default_is_none(_no_glm_env) -> None:  # type: ignore[no-untyped-def]
    """Field is None when CLAUDE_SWITCH_GLM_BASE_URL is not set."""
    # Use _env_file=None to skip reading the project .env file (which may have
    # CLAUDE_SWITCH_GLM_BASE_URL set for local development).
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.anthropic_api_base is None


def test_anthropic_api_base_populated_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """CLAUDE_SWITCH_GLM_BASE_URL is picked up and stored."""
    monkeypatch.setenv("CLAUDE_SWITCH_GLM_BASE_URL", "https://open.bigmodel.cn/api/anthropic")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.anthropic_api_base == "https://open.bigmodel.cn/api/anthropic"


def test_anthropic_api_base_blank_is_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A blank env var is coerced to None by the _blank_to_none validator."""
    monkeypatch.setenv("CLAUDE_SWITCH_GLM_BASE_URL", "   ")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.anthropic_api_base is None


def test_anthropic_api_base_rejects_http_scheme(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Non-HTTPS proxy URL must be rejected at settings load time."""
    monkeypatch.setenv("CLAUDE_SWITCH_GLM_BASE_URL", "http://open.bigmodel.cn/api/anthropic")
    with pytest.raises(Exception, match="https://"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_anthropic_api_base_rejects_private_ip(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Private-range IP URLs must be rejected to prevent SSRF."""
    monkeypatch.setenv("CLAUDE_SWITCH_GLM_BASE_URL", "https://192.168.1.1/api")
    with pytest.raises(Exception, match="private"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_anthropic_api_base_rejects_loopback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Loopback URLs must be rejected to prevent SSRF."""
    monkeypatch.setenv("CLAUDE_SWITCH_GLM_BASE_URL", "https://127.0.0.1/api")
    with pytest.raises(Exception, match=r"private|loopback"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_anthropic_api_base_rejects_metadata_endpoint(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Cloud metadata IP must be rejected to prevent SSRF."""
    monkeypatch.setenv("CLAUDE_SWITCH_GLM_BASE_URL", "https://169.254.169.254/latest/meta-data")
    with pytest.raises(Exception, match=r"169\.254\.169\.254|metadata"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_anthropic_api_base_accepts_valid_https_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A valid public HTTPS URL is accepted without error."""
    monkeypatch.setenv("CLAUDE_SWITCH_GLM_BASE_URL", "https://open.bigmodel.cn/api/anthropic")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.anthropic_api_base == "https://open.bigmodel.cn/api/anthropic"
