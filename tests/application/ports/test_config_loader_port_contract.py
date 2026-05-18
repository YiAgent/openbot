"""Contract test — FakeConfigLoader returns programmed configs."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from openbot.domain.config_schema import EffectiveConfig
from openbot.infrastructure.config_loader import baked_in_defaults
from tests._fakes.config_loader import FakeConfigLoader


def _make_event(repo: str = "owner/repo") -> object:
    """Minimal event stub — only `repo` is needed by FakeConfigLoader."""
    event = AsyncMock()
    event.repo = repo
    return event


@pytest.mark.asyncio
async def test_returns_programmed_config() -> None:
    cfg = baked_in_defaults()
    loader = FakeConfigLoader(by_repo={"owner/repo": cfg})
    result = await loader.load_for_repo(adapter=None, event=_make_event("owner/repo"))
    assert result is cfg
    assert loader.calls == ["owner/repo"]


@pytest.mark.asyncio
async def test_returns_default_when_repo_missing() -> None:
    default = baked_in_defaults()
    loader = FakeConfigLoader(default=default)
    result = await loader.load_for_repo(adapter=None, event=_make_event("owner/missing"))
    assert result is default
    assert loader.calls == ["owner/missing"]


@pytest.mark.asyncio
async def test_falls_back_to_baked_in_defaults() -> None:
    loader = FakeConfigLoader()
    result = await loader.load_for_repo(adapter=None, event=_make_event("any/repo"))
    assert isinstance(result, EffectiveConfig)
    assert loader.calls == ["any/repo"]
