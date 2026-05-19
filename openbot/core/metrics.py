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


# ── Metric declarations ───────────────────────────────────────────────────────

try:
    from prometheus_client import Counter, Gauge  # type: ignore[import-untyped]

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

    workflow_total = WorkflowCounter()
    llm_cost_usd_total = CostCounter()
    queue_depth = QueueGauge()

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

    workflow_total = SentryOnlyWorkflowCounter()
    llm_cost_usd_total = SentryOnlyCostCounter()
    queue_depth = SentryOnlyQueueGauge()


__all__ = ["llm_cost_usd_total", "queue_depth", "workflow_total"]
