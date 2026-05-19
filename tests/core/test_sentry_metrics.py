"""Unit tests for openbot.core.sentry_metrics.Metrics shim."""

from __future__ import annotations

from openbot.core.sentry_metrics import Metrics


def test_metrics_has_no_set_method() -> None:
    """Metrics.set() was removed because sentry_sdk.metrics has no 'set'
    function in 2.x — callers were silently dropping data with no error."""
    assert not hasattr(Metrics(), "set")
