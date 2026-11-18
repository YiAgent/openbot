"""Route-registration guards for the FastAPI app.

These pin that each channel webhook is actually mounted — the kind of
regression where an adapter + route module exist but were never wired into
`include_router`, so the endpoint silently 404s in production.
"""

from __future__ import annotations

import pytest

from openbot.entrypoints.api.app import app


def _route_paths() -> set[str]:
    return {getattr(r, "path", "") for r in app.routes}


@pytest.mark.unit
def test_github_webhook_route_registered() -> None:
    assert "/webhook/github" in _route_paths()


@pytest.mark.unit
def test_linear_webhook_route_registered() -> None:
    assert "/webhook/linear" in _route_paths()


@pytest.mark.unit
def test_health_route_registered() -> None:
    assert "/health" in _route_paths()
