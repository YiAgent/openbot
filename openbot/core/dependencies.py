"""Application-level dependency factories.

Provides ``build_sandbox_cache`` — the single place that maps
``Settings`` to the correct ``SandboxCachePort`` implementation.

Design:
  - Phase 1 default is OFF (``sandbox_cache_enabled=False`` → ``NoOpSandboxCache``).
  - When enabled AND the feature is in the allow list AND ``daytona_api_key``
    is set → ``DaytonaSnapshotCache``.
  - When enabled AND feature matches BUT ``daytona_api_key`` is unset →
    raises ``ValueError`` (explicit misconfiguration error; silent degradation
    would mask ops mistakes).
  - When enabled BUT feature is not in the allow list → ``NoOpSandboxCache``
    (gradual per-feature rollout without code changes).

Per Task 4.1 of ``docs/superpowers/plans/2026-05-21-sandbox-snapshot-cache-plan.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbot.application.ports.sandbox_cache import SandboxCachePort
    from openbot.core.settings import Settings


def build_sandbox_cache(settings: Settings, *, feature: str) -> SandboxCachePort:
    """Return the appropriate ``SandboxCachePort`` for this deployment and feature.

    Args:
        settings:  The effective ``Settings`` instance (call-site may inject a
                   test override; production callers pass ``get_settings()``).
        feature:   The workflow feature name (e.g. ``"chat"``, ``"fix"``).
                   Compared against ``settings.sandbox_cache_features``.

    Returns:
        ``DaytonaSnapshotCache`` when the cache is enabled, the feature matches,
        and ``daytona_api_key`` is set.
        ``NoOpSandboxCache`` otherwise (no-op is always safe).

    Raises:
        ``ValueError``: when the cache is enabled AND the feature matches BUT
        ``daytona_api_key`` is unset — this is a misconfiguration that ops
        must fix, not something we should silently degrade past.
    """
    from openbot.infrastructure.sandboxes.cache_noop import NoOpSandboxCache

    if not settings.sandbox_cache_enabled:
        return NoOpSandboxCache()

    # Parse the comma-separated feature list stored as a raw string.
    allowed_features = frozenset(
        f.strip() for f in settings.sandbox_cache_features.split(",") if f.strip()
    )
    if feature not in allowed_features:
        return NoOpSandboxCache()

    # Feature matches — we need a real Daytona cache.
    if settings.daytona_api_key is None:
        raise ValueError(
            "sandbox_cache_enabled=True and feature is in sandbox_cache_features, "
            "but daytona_api_key is unset. Set OPENBOT_DAYTONA_API_KEY before "
            "enabling the sandbox cache."
        )

    from openbot.infrastructure.sandboxes.cache_daytona import DaytonaSnapshotCache
    from openbot.infrastructure.sandboxes.daytona import _build_client

    client = _build_client(
        settings.daytona_api_key.get_secret_value(),
        settings.daytona_server_url,
    )
    return DaytonaSnapshotCache(
        daytona_client=client,
        ttl_seconds=settings.sandbox_cache_ttl_seconds,
        max_entries=settings.sandbox_cache_max_entries,
    )


__all__ = ["build_sandbox_cache"]
