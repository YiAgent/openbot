"""Prometheus cache metrics — registration + call-surface tests.

The cache metrics module exposes three new symbols to the dispatcher
and the cache adapters:

  ``sandbox_cache_total``         — Counter{feature, result}
  ``sandbox_cache_publish_total`` — Counter{feature, result}
  ``sandbox_cache_acquire_seconds`` — Histogram{result}

Each test pins one invariant in the lightest way possible: call the
exported symbol's ``labels(...).inc()`` (or ``.observe()``) and verify
it doesn't raise.  Whether ``prometheus_client`` is installed or not,
the stubs must absorb the call cleanly — the dispatcher never guards
against ``prometheus_client`` being absent.

Per Task 2.2 of
``docs/superpowers/plans/2026-05-21-sandbox-snapshot-cache-plan.md``.
"""

from __future__ import annotations

from openbot.core.metrics import (
    sandbox_cache_acquire_seconds,
    sandbox_cache_publish_total,
    sandbox_cache_total,
)


def test_sandbox_cache_total_accepts_feature_and_result_labels() -> None:
    """Dispatcher calls
    ``sandbox_cache_total.labels(feature="fix", result="hit").inc()``
    on every acquire path. The symbol must accept those label kwargs and
    not raise regardless of whether prometheus_client is installed.
    """
    sandbox_cache_total.labels(feature="fix", result="hit").inc()
    sandbox_cache_total.labels(feature="triage", result="miss").inc()
    sandbox_cache_total.labels(feature="review", result="backend_error").inc()


def test_sandbox_cache_publish_total_accepts_feature_and_result_labels() -> None:
    """Dispatcher calls
    ``sandbox_cache_publish_total.labels(feature="fix", result="created").inc()``
    after a successful cold-path publish. Must accept those labels without
    raising.
    """
    sandbox_cache_publish_total.labels(feature="fix", result="created").inc()
    sandbox_cache_publish_total.labels(feature="fix", result="failed").inc()


def test_sandbox_cache_acquire_seconds_accepts_result_label_and_observe() -> None:
    """Dispatcher records acquire latency via
    ``sandbox_cache_acquire_seconds.labels(result="hit").observe(seconds)``.
    The symbol must accept a ``result`` label and an ``observe()`` call.
    """
    sandbox_cache_acquire_seconds.labels(result="hit").observe(0.05)
    sandbox_cache_acquire_seconds.labels(result="miss").observe(10.2)
