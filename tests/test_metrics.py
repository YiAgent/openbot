"""Prometheus /metrics endpoint contract test.

We only validate that:

  1. ``/metrics`` exists and returns 200 (so Heroku metrics scrape /
     a future Prometheus scrape won't 404 silently).
  2. The body is in Prometheus exposition format (``text/plain;
     version=0.0.4`` content type or ``# HELP`` prefix).
  3. The /health probe stays unmeasured (we excluded it deliberately —
     actually we excluded /metrics itself; /health is fine to measure
     but most teams skip it to reduce series cardinality. For now we
     just assert /health remains 200 and unchanged).

We do NOT assert specific metric names; those are owned by the
instrumentator and may change between versions.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from openbot.webapp import app


def test_metrics_endpoint_returns_200() -> None:
    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 200


def test_metrics_endpoint_returns_prometheus_exposition() -> None:
    with TestClient(app) as client:
        response = client.get("/metrics")
    content_type = response.headers.get("content-type", "")
    assert "text/plain" in content_type, f"unexpected content-type: {content_type}"
    # Exposition format: every metric block starts with a `# HELP` line.
    # If the body is empty or HTML we're not actually serving Prometheus
    # data — the instrumentator may have been mis-wired.
    body = response.text
    assert (
        body.startswith("# HELP") or "\n# HELP " in body
    ), f"body does not look like Prometheus exposition: {body[:200]}"


def test_metrics_endpoint_does_not_break_health() -> None:
    """Smoke check: mounting /metrics didn't shadow /health."""
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
