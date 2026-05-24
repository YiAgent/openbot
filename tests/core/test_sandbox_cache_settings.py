"""Settings + DI wiring for the sandbox snapshot cache (Task 4.1).

Covers:
  - ``sandbox_cache_enabled`` defaults to False.
  - ``sandbox_cache_features`` parses from comma-separated env var.
  - ``sandbox_cache_max_entries`` and ``sandbox_cache_ttl_seconds`` defaults.
  - ``build_sandbox_cache(settings, feature)`` returns ``NoOpSandboxCache``
    when the cache is disabled.
  - Returns ``NoOpSandboxCache`` when enabled but the feature is not in the
    allowed list.
  - Returns ``DaytonaSnapshotCache`` when enabled, feature matches, and
    ``daytona_api_key`` is set.
  - Raises ``ValueError`` when enabled but ``daytona_api_key`` is unset
    (can't build a Daytona cache without credentials).
"""

from __future__ import annotations

import pytest

from openbot.core.settings import Settings, get_settings
from openbot.infrastructure.sandboxes.cache_noop import NoOpSandboxCache


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:  # type: ignore[return]
    """Ensure Settings cache is cleared around each test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── Settings field defaults ───────────────────────────────────────────────────


def test_sandbox_cache_disabled_by_default() -> None:
    """Cache is off by default — Phase 1 rollout keeps production cold."""
    s = Settings()
    assert s.sandbox_cache_enabled is False


def test_sandbox_cache_features_default_is_empty_string() -> None:
    """No features have the cache enabled by default (raw string is empty)."""
    s = Settings()
    assert s.sandbox_cache_features == ""


def test_sandbox_cache_features_stores_raw_comma_separated_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OPENBOT_SANDBOX_CACHE_FEATURES=chat,fix`` is stored as-is.

    Parsing into a set is done by ``build_sandbox_cache`` in
    ``openbot.application.sandbox_cache_deps``.
    """
    monkeypatch.setenv("OPENBOT_SANDBOX_CACHE_FEATURES", "chat,fix")
    s = Settings()
    assert "chat" in s.sandbox_cache_features
    assert "fix" in s.sandbox_cache_features


def test_sandbox_cache_max_entries_default() -> None:
    s = Settings()
    assert s.sandbox_cache_max_entries == 50


def test_sandbox_cache_ttl_seconds_default() -> None:
    s = Settings()
    assert s.sandbox_cache_ttl_seconds == 86_400


# ── build_sandbox_cache factory ───────────────────────────────────────────────


def test_build_sandbox_cache_returns_noop_when_disabled() -> None:
    """``sandbox_cache_enabled=False`` → ``build_sandbox_cache`` returns
    a ``NoOpSandboxCache`` regardless of feature."""
    from openbot.application.sandbox_cache_deps import build_sandbox_cache

    s = Settings()
    cache = build_sandbox_cache(s, feature="triage")
    assert isinstance(cache, NoOpSandboxCache)


def test_build_sandbox_cache_returns_noop_when_feature_not_in_allow_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache enabled for ``chat`` only — ``triage`` still gets ``NoOpSandboxCache``."""
    from openbot.application.sandbox_cache_deps import build_sandbox_cache

    monkeypatch.setenv("OPENBOT_SANDBOX_CACHE_ENABLED", "true")
    monkeypatch.setenv("OPENBOT_SANDBOX_CACHE_FEATURES", "chat")
    monkeypatch.setenv("OPENBOT_DAYTONA_API_KEY", "fake-key-for-test")
    s = Settings()
    cache = build_sandbox_cache(s, feature="triage")
    assert isinstance(cache, NoOpSandboxCache)


def test_build_sandbox_cache_returns_daytona_cache_when_enabled_and_feature_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sandbox_cache_enabled=True`` + feature in allow list + daytona_api_key set
    → ``DaytonaSnapshotCache`` is returned."""
    from openbot.application.sandbox_cache_deps import build_sandbox_cache
    from openbot.infrastructure.sandboxes.cache_daytona import DaytonaSnapshotCache

    monkeypatch.setenv("OPENBOT_SANDBOX_CACHE_ENABLED", "true")
    monkeypatch.setenv("OPENBOT_SANDBOX_CACHE_FEATURES", "fix")
    monkeypatch.setenv("OPENBOT_DAYTONA_API_KEY", "fake-key-for-test")
    s = Settings()
    cache = build_sandbox_cache(s, feature="fix")
    assert isinstance(cache, DaytonaSnapshotCache)


def test_build_sandbox_cache_raises_when_enabled_but_no_daytona_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sandbox_cache_enabled=True`` + feature matches but ``daytona_api_key``
    is not set → raises ``ValueError`` (can't build a Daytona cache without creds).

    Ops must set the key *before* enabling the cache to prevent silent
    degradation from masking a misconfiguration.
    """
    from openbot.application.sandbox_cache_deps import build_sandbox_cache

    monkeypatch.setenv("OPENBOT_SANDBOX_CACHE_ENABLED", "true")
    monkeypatch.setenv("OPENBOT_SANDBOX_CACHE_FEATURES", "fix")
    # Explicitly unset key (might be set in the environment).
    monkeypatch.delenv("OPENBOT_DAYTONA_API_KEY", raising=False)
    s = Settings(daytona_api_key=None)

    with pytest.raises(ValueError, match="daytona_api_key"):
        build_sandbox_cache(s, feature="fix")
