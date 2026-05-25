"""Unit tests for build_sandbox_factory composition-root helper.

These tests verify the routing logic:

  - daytona_api_key unset  → return None  (fix loop degrades gracefully)
  - daytona_api_key set    → return an asynccontextmanager factory that
                             yields DaytonaSandboxAdapter and closes on exit

The Daytona SDK + adapter are never imported in the "key absent" path,
so the tests can run without the optional daytona package installed.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openbot.application.sandbox_factory_deps import build_sandbox_factory
from openbot.core.settings import Settings

# ── helper ─────────────────────────────────────────────────────────────────


def _settings(**kwargs: Any) -> Settings:
    """Build a minimal Settings overriding only the provided keys."""
    return Settings(github_secret="x", github_app_id=1, **kwargs)


# ── key absent ─────────────────────────────────────────────────────────────


def test_returns_none_when_daytona_api_key_unset() -> None:
    """No API key → no factory; the caller treats None as 'no sandbox'."""
    s = _settings()
    assert s.daytona_api_key is None
    result = build_sandbox_factory(s)
    assert result is None


# ── key present ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_callable_when_daytona_api_key_set() -> None:
    """With an API key the factory is a callable (not already a context manager)."""
    s = _settings(daytona_api_key="fake-key")
    factory = build_sandbox_factory(s)
    assert factory is not None
    assert callable(factory)


@pytest.mark.asyncio
async def test_factory_yields_adapter_and_closes() -> None:
    """The factory must enter the async context, yield the adapter, then close."""
    s = _settings(daytona_api_key="fake-key")
    factory = build_sandbox_factory(s)
    assert factory is not None

    fake_adapter = MagicMock()
    fake_adapter.close = AsyncMock()

    with patch(
        "openbot.infrastructure.sandboxes.daytona.DaytonaSandboxAdapter.create",
        new=AsyncMock(return_value=fake_adapter),
    ):
        ctx = factory()
        assert isinstance(ctx, AbstractAsyncContextManager)
        async with ctx as adapter:
            assert adapter is fake_adapter
        # close() must be called after the block exits normally
        fake_adapter.close.assert_called_once()


@pytest.mark.asyncio
async def test_factory_closes_on_exception() -> None:
    """close() is called even when an exception propagates out of the with block."""
    s = _settings(daytona_api_key="fake-key")
    factory = build_sandbox_factory(s)
    assert factory is not None

    fake_adapter = MagicMock()
    fake_adapter.close = AsyncMock()

    with patch(
        "openbot.infrastructure.sandboxes.daytona.DaytonaSandboxAdapter.create",
        new=AsyncMock(return_value=fake_adapter),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            async with factory():
                raise RuntimeError("boom")
        fake_adapter.close.assert_called_once()
