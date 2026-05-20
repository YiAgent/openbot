"""Business metrics shim for Sentry Metrics.

Provides a clean API for recording counters, distributions, and gauges
without the rest of the app needing to import ``sentry_sdk`` directly.

Example::

    from openbot.core.sentry_metrics import metrics

    metrics.incr("workflow_started", tags={"type": "triage"})
    metrics.distribution("llm_latency_ms", 340.0, unit="millisecond")
    metrics.gauge("queue_depth", 7.0)

If Sentry is not initialised (e.g. local dev without DSN), these calls
become silent no-ops.

Note: sentry_sdk ≥ 2.x dropped the ``set`` metric type from its public
API. Use ``distribution()`` for approximate unique-count approximations.
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

            # Sentry 2.x uses 'count' for counters and 'attributes' for tags.
            metrics.count(key, value, unit=unit, attributes=tags or {})
        except (ImportError, RuntimeError, AttributeError):
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

            metrics.distribution(key, value, unit=unit, attributes=tags or {})
        except (ImportError, RuntimeError, AttributeError):
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

            metrics.gauge(key, value, unit=unit, attributes=tags or {})
        except (ImportError, RuntimeError, AttributeError):
            pass


metrics = Metrics()
