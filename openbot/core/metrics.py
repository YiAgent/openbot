"""Prometheus business metrics for OpenBot.

Three counters covering the signals most useful for ops + cost dashboards:

  openbot_workflow_total{feature, outcome}
      Counts every workflow execution by feature (triage/review/fix/chat)
      and outcome (completed/failed/skipped).  Wired by ``audit_lifecycle``
      so every code path through the workflow layer is covered uniformly.

  openbot_llm_cost_usd_total{feature}
      Cumulative LLM spend in USD.  Wired by ``llm.complete`` at the point
      where cost is extracted from the LiteLLM response — the ground truth
      before any Postgres persistence.

  openbot_queue_depth
      Gauge: number of entries currently in the ``openbot:workflows`` Redis
      stream (XLEN).  Updated once per ``_read_and_dispatch`` round; gives
      ops a "queue is growing" signal without requiring Prometheus to scrape
      Redis directly.

Design notes:

  - Import-safe: the module builds the metric objects at import time using
    ``try/except``.  If ``prometheus_client`` is not installed (a slim test
    image), all three symbols become no-op stubs that accept any call.  The
    callers never need to guard their ``increment`` / ``set`` calls.

  - Label cardinality is kept intentionally low.  ``feature`` has four values
    (triage, review, fix, chat) and ``outcome`` has three (completed, failed,
    skipped).  No per-repo or per-actor labels — those belong in the
    structured log rather than the metric cardinality.

  - ``prometheus_client`` is a transitive dependency of
    ``prometheus-fastapi-instrumentator``, so it is always present in
    production.  The guard below is a safety net for stripped CI images.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


# ── Public stubs ──────────────────────────────────────────────────────────────
# Defined before the try-block so type checkers see the real type.


class _NoOpCounter:
    """Drop-in stub when prometheus_client is absent."""

    def labels(self, **_: Any) -> _NoOpCounter:
        return self

    def inc(self, amount: float = 1) -> None:
        del amount  # intentionally unused — no-op stub


class _NoOpGauge:
    """Drop-in stub when prometheus_client is absent."""

    def set(self, value: float) -> None:
        del value  # intentionally unused — no-op stub

    def inc(self, amount: float = 1) -> None:
        del amount  # intentionally unused — no-op stub

    def dec(self, amount: float = 1) -> None:
        del amount  # intentionally unused — no-op stub


class _NoOpHistogram:
    """Drop-in stub when prometheus_client is absent."""

    def labels(self, **_: Any) -> _NoOpHistogram:
        return self

    def observe(self, amount: float) -> None:
        del amount  # intentionally unused — no-op stub


# ── Metric declarations ───────────────────────────────────────────────────────

try:
    from prometheus_client import Counter, Gauge, Histogram  # type: ignore[import-untyped]

    _workflow_total = Counter(
        "openbot_workflow_total",
        "Total workflow executions by feature and outcome",
        ["feature", "outcome"],
    )
    _llm_cost_usd_total = Counter(
        "openbot_llm_cost_usd_total",
        "Cumulative LLM spend in USD",
        ["feature"],
    )
    _queue_depth = Gauge(
        "openbot_queue_depth",
        "Number of entries in the openbot:workflows Redis stream",
    )
    # Unified-sandbox-entry slice. Tracks each dispatch's sandbox
    # provisioning decision so ops dashboards can answer "how often do
    # we open a sandbox vs bypass" and "which bypass source dominates"
    # without scraping logs. ``bypass_source`` cardinality is bounded:
    # ``none`` (happy path), ``static`` (router said NO_SANDBOX),
    # ``classifier`` (OR-merge bypassed), ``degrade`` (provisioning
    # tried and failed).
    _dispatch_sandbox_total = Counter(
        "openbot_dispatch_sandbox_total",
        "Sandbox provisioning outcomes per dispatch",
        ["feature", "policy", "bypass_source"],
    )
    # Fail-open counter for the LLM intent classifier. Increments once
    # per swallowed exception in ``classify_for_dispatch``; that helper
    # never re-raises, so without this metric the only sign of a
    # regressing classifier is a buried WARN log.
    _classifier_error_total = Counter(
        "openbot_classifier_error_total",
        "Classifier exceptions swallowed by the fail-open wrap",
        ["feature"],
    )
    # Snapshot cache acquire outcomes. ``result`` ∈ {hit, miss, stale,
    # backend_error}. ``feature`` scopes the counter to one workflow so
    # dashboards can answer "which feature benefits most from caching".
    _sandbox_cache_total = Counter(
        "openbot_sandbox_cache_total",
        "Sandbox cache acquire outcomes per feature and result",
        ["feature", "result"],
    )
    # Snapshot cache publish outcomes. ``result`` ∈ {created, skipped,
    # failed}. Emitted after the cold-path clone completes.
    _sandbox_cache_publish_total = Counter(
        "openbot_sandbox_cache_publish_total",
        "Sandbox cache publish outcomes per feature and result",
        ["feature", "result"],
    )
    # End-to-end acquire latency from the dispatcher's perspective —
    # includes the _refresh_to_ref git I/O on a hit. Labelled by
    # ``result`` so hit vs miss distributions are separate series.
    _sandbox_cache_acquire_seconds = Histogram(
        "openbot_sandbox_cache_acquire_seconds",
        "Sandbox cache acquire duration in seconds",
        ["result"],
        buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    )

    class _LabelledSentryHistogram:
        """Prometheus Histogram + Sentry distribution mirror.

        Wraps a Histogram the same way ``_LabelledSentryCounter`` wraps a
        Counter — ``labels()`` captures kwargs, ``observe()`` writes to
        both the Prometheus bucket and the Sentry distribution channel.
        """

        def __init__(self, histogram: Histogram, sentry_name: str) -> None:
            self._histogram = histogram
            self._sentry_name = sentry_name
            self._kwargs: dict[str, str] = {}

        def labels(self, **kwargs: Any) -> _LabelledSentryHistogram:
            self._kwargs = kwargs
            return self

        def observe(self, amount: float) -> None:
            self._histogram.labels(**self._kwargs).observe(amount)
            from openbot.core.sentry_metrics import metrics

            metrics.distribution(self._sentry_name, amount, tags=self._kwargs)

    class WorkflowCounter:
        def labels(self, **kwargs: Any) -> WorkflowCounter:
            self._kwargs = kwargs
            return self

        def inc(self, amount: float = 1) -> None:
            _workflow_total.labels(**self._kwargs).inc(amount)
            from openbot.core.sentry_metrics import metrics

            metrics.incr("workflow_total", amount, tags=self._kwargs)

    class CostCounter:
        def labels(self, **kwargs: Any) -> CostCounter:
            self._kwargs = kwargs
            return self

        def inc(self, amount: float = 1) -> None:
            _llm_cost_usd_total.labels(**self._kwargs).inc(amount)
            from openbot.core.sentry_metrics import metrics

            metrics.incr("llm_cost_usd", amount, tags=self._kwargs)

    class QueueGauge:
        def set(self, value: float) -> None:
            _queue_depth.set(value)
            from openbot.core.sentry_metrics import metrics

            metrics.gauge("queue_depth", value)

        def inc(self, amount: float = 1) -> None:
            _queue_depth.inc(amount)

        def dec(self, amount: float = 1) -> None:
            _queue_depth.dec(amount)

    class _LabelledSentryCounter:
        """Generic Prometheus + Sentry mirror for labelled counters.

        Used by metrics that are too narrow to warrant a hand-rolled
        wrapper class but still want the Sentry side-channel for
        production observability. Constructor takes the underlying
        prometheus counter + the Sentry metric name; ``labels()``
        captures the kwargs for the next ``inc()``.
        """

        def __init__(self, counter: Counter, sentry_name: str) -> None:
            self._counter = counter
            self._sentry_name = sentry_name
            self._kwargs: dict[str, str] = {}

        def labels(self, **kwargs: Any) -> _LabelledSentryCounter:
            self._kwargs = kwargs
            return self

        def inc(self, amount: float = 1) -> None:
            self._counter.labels(**self._kwargs).inc(amount)
            from openbot.core.sentry_metrics import metrics

            metrics.incr(self._sentry_name, amount, tags=self._kwargs)

    workflow_total = WorkflowCounter()
    llm_cost_usd_total = CostCounter()
    queue_depth = QueueGauge()
    dispatch_sandbox_total = _LabelledSentryCounter(_dispatch_sandbox_total, "dispatch_sandbox")
    classifier_error_total = _LabelledSentryCounter(_classifier_error_total, "classifier_error")
    sandbox_cache_total = _LabelledSentryCounter(_sandbox_cache_total, "sandbox_cache")
    sandbox_cache_publish_total = _LabelledSentryCounter(
        _sandbox_cache_publish_total, "sandbox_cache_publish"
    )
    sandbox_cache_acquire_seconds = _LabelledSentryHistogram(
        _sandbox_cache_acquire_seconds, "sandbox_cache_acquire_seconds"
    )

except ImportError:
    _logger.warning("prometheus_client_not_installed_metrics_disabled")
    from openbot.core.sentry_metrics import metrics

    class SentryOnlyWorkflowCounter:
        def labels(self, **kwargs: Any) -> SentryOnlyWorkflowCounter:
            self._kwargs = kwargs
            return self

        def inc(self, amount: float = 1) -> None:
            metrics.incr("workflow_total", amount, tags=self._kwargs)

    class SentryOnlyCostCounter:
        def labels(self, **kwargs: Any) -> SentryOnlyCostCounter:
            self._kwargs = kwargs
            return self

        def inc(self, amount: float = 1) -> None:
            metrics.incr("llm_cost_usd", amount, tags=self._kwargs)

    class SentryOnlyQueueGauge:
        def set(self, value: float) -> None:
            metrics.gauge("queue_depth", value)

        def inc(self, amount: float = 1) -> None:
            pass

        def dec(self, amount: float = 1) -> None:
            pass

    class SentryOnlyLabelledCounter:
        """Sentry-only mirror for ``dispatch_sandbox_total`` /
        ``classifier_error_total`` when ``prometheus_client`` is absent
        (slim CI image). Same protocol as the Prometheus-backed
        ``_LabelledSentryCounter`` so call sites don't branch."""

        def __init__(self, sentry_name: str) -> None:
            self._sentry_name = sentry_name
            self._kwargs: dict[str, str] = {}

        def labels(self, **kwargs: Any) -> SentryOnlyLabelledCounter:
            self._kwargs = kwargs
            return self

        def inc(self, amount: float = 1) -> None:
            metrics.incr(self._sentry_name, amount, tags=self._kwargs)

    workflow_total = SentryOnlyWorkflowCounter()
    llm_cost_usd_total = SentryOnlyCostCounter()
    queue_depth = SentryOnlyQueueGauge()
    dispatch_sandbox_total = SentryOnlyLabelledCounter("dispatch_sandbox")
    classifier_error_total = SentryOnlyLabelledCounter("classifier_error")

    class SentryOnlyLabelledHistogram:
        """Sentry-only histogram stub when ``prometheus_client`` is absent.

        Same ``labels() → observe()`` protocol as ``_LabelledSentryHistogram``
        so call sites in the dispatcher never branch on prometheus availability.
        """

        def __init__(self, sentry_name: str) -> None:
            self._sentry_name = sentry_name
            self._kwargs: dict[str, str] = {}

        def labels(self, **kwargs: Any) -> SentryOnlyLabelledHistogram:
            self._kwargs = kwargs
            return self

        def observe(self, amount: float) -> None:
            metrics.distribution(self._sentry_name, amount, tags=self._kwargs)

    sandbox_cache_total = SentryOnlyLabelledCounter("sandbox_cache")
    sandbox_cache_publish_total = SentryOnlyLabelledCounter("sandbox_cache_publish")
    sandbox_cache_acquire_seconds = SentryOnlyLabelledHistogram("sandbox_cache_acquire_seconds")


__all__ = [
    "classifier_error_total",
    "dispatch_sandbox_total",
    "llm_cost_usd_total",
    "queue_depth",
    "sandbox_cache_acquire_seconds",
    "sandbox_cache_publish_total",
    "sandbox_cache_total",
    "workflow_total",
]
