"""Unit tests for openbot.core.sentry_metrics.Metrics shim."""

from __future__ import annotations

import builtins
from unittest.mock import patch

from openbot.core.sentry_metrics import Metrics


def test_metrics_has_no_set_method() -> None:
    """Metrics.set() was removed because sentry_sdk.metrics has no 'set'
    function in 2.x — callers were silently dropping data with no error."""
    assert not hasattr(Metrics(), "set")


# ── Happy-path: correct SDK delegation ───────────────────────────────────────


def test_incr_calls_sentry_count() -> None:
    """Metrics.incr() must delegate to sentry_sdk.metrics.count() with
    matching name, value, unit, and attributes."""
    m = Metrics()
    with patch("sentry_sdk.metrics") as mock_sdk_metrics:
        m.incr("workflow.started", 2.0, tags={"feature": "triage"})
    mock_sdk_metrics.count.assert_called_once_with(
        "workflow.started",
        2.0,
        unit="none",
        attributes={"feature": "triage"},
    )


def test_distribution_calls_sentry_distribution() -> None:
    """Metrics.distribution() must delegate to sentry_sdk.metrics.distribution()."""
    m = Metrics()
    with patch("sentry_sdk.metrics") as mock_sdk_metrics:
        m.distribution("http.request.duration", 12.3, unit="millisecond")
    mock_sdk_metrics.distribution.assert_called_once_with(
        "http.request.duration",
        12.3,
        unit="millisecond",
        attributes={},
    )


def test_gauge_calls_sentry_gauge() -> None:
    """Metrics.gauge() must delegate to sentry_sdk.metrics.gauge()."""
    m = Metrics()
    with patch("sentry_sdk.metrics") as mock_sdk_metrics:
        m.gauge("queue_depth", 7.0, tags={"stream": "openbot:workflows"})
    mock_sdk_metrics.gauge.assert_called_once_with(
        "queue_depth",
        7.0,
        unit="none",
        attributes={"stream": "openbot:workflows"},
    )


# ── Resilience: SDK errors are silenced ──────────────────────────────────────


def test_incr_swallows_attribute_error() -> None:
    """If sentry_sdk is installed but not yet initialised, metrics.count
    raises AttributeError. Metrics.incr() must not propagate it."""
    m = Metrics()
    with patch("sentry_sdk.metrics") as mock_sdk_metrics:
        mock_sdk_metrics.count.side_effect = AttributeError("no active hub")
        m.incr("k")  # must not raise


def test_incr_swallows_runtime_error() -> None:
    """If sentry_sdk raises RuntimeError (e.g. misconfigured hub),
    Metrics.incr() must not propagate it."""
    m = Metrics()
    with patch("sentry_sdk.metrics") as mock_sdk_metrics:
        mock_sdk_metrics.count.side_effect = RuntimeError("Sentry not ready")
        m.incr("k")  # must not raise


def test_incr_swallows_import_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If sentry_sdk is not installed (slim CI image), Metrics.incr()
    must not raise — same graceful degradation pattern as init_sentry."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sentry_sdk" or name.startswith("sentry_sdk."):
            raise ImportError(f"mocked absent: {name}")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    Metrics().incr("k")  # must not raise
