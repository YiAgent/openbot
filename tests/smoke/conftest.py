"""Smoke-layer conftest.

Provides a minimal environment to boot Settings, the FastAPI app,
and the worker without real external connections.

Forbidden: full business assembly, real network, real DB/Redis URLs.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def boot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum env vars required to construct Settings().

    Tests that need additional vars call monkeypatch.setenv() after
    requesting this fixture.
    """
    monkeypatch.setenv("OPENBOT_ENV", "test")
    monkeypatch.setenv("OPENBOT_GITHUB_APP_ID", "0")
    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("OPENBOT_GITHUB_PRIVATE_KEY", "")
