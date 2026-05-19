"""Business metrics shim for Sentry Metrics.

Provides a clean API for recording counters, distributions, and gauges
without the rest of the app needing to import ``sentry_sdk`` directly.

Example::

    from openbot.infrastructure.metrics import metrics

    metrics.incr("workflow_started", tags={"type": "triage"})

If Sentry is not initialised (e.g. local dev without DSN), these calls
become no-ops.
"""

from __future__ import annotations

from typing import Any


class Metrics:
    """Namespace for Sentry metrics with graceful fallback."""

    def incr(
        self,
        key: str,
        value: float = 1.0,
        unit: str = "none",
        tags: dict[str, Any] | None = None,
    ) -> None:
        """Increment a counter."""
        try:
            from sentry_sdk import metrics

            metrics.incr(key, value, unit=unit, tags=tags or {})
        except (ImportError, RuntimeError):
            pass

    def distribution(
        self,
        key: str,
        value: float,
        unit: str = "none",
        tags: dict[str, Any] | None = None,
    ) -> None:
        """Record a distribution (e.g. latency, token count)."""
        try:
            from sentry_sdk import metrics

            metrics.distribution(key, value, unit=unit, tags=tags or {})
        except (ImportError, RuntimeError):
            pass

    def gauge(
        self,
        key: str,
        value: float,
        unit: str = "none",
        tags: dict[str, Any] | None = None,
    ) -> None:
        """Record a gauge (e.g. queue depth)."""
        try:
            from sentry_sdk import metrics

            metrics.gauge(key, value, unit=unit, tags=tags or {})
        except (ImportError, RuntimeError):
            pass

    def set(
        self,
        key: str,
        value: Any,
        unit: str = "none",
        tags: dict[str, Any] | None = None,
    ) -> None:
        """Record a set (unique counts, e.g. user_id)."""
        try:
            from sentry_sdk import metrics

            metrics.set(key, value, unit=unit, tags=tags or {})
        except (ImportError, RuntimeError):
            pass


metrics = Metrics()
