"""Smoke test — confirms FastAPI app boots and /health responds."""

from fastapi.testclient import TestClient

from openbot import __version__
from openbot.webapp import app


def test_health_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"] == __version__


def test_health_is_cheap() -> None:
    """/health must remain side-effect-free (PRD §5.1 liveness)."""
    client = TestClient(app)
    # Hammering it 50 times must not change observable state.
    for _ in range(50):
        assert client.get("/health").status_code == 200
