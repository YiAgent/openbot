"""agent_checkpointer context manager — unit test using MemorySaver stub."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest


async def test_agent_checkpointer_yields_saver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Context manager should yield the saver and call setup()."""
    from openbot.infrastructure.persistence import agent_checkpointer as mod

    setup_called: list[bool] = []

    class _FakeSaver:
        async def setup(self) -> None:
            setup_called.append(True)

    @asynccontextmanager
    async def _fake_from_conn_string(dsn: str) -> AsyncGenerator[_FakeSaver, None]:
        yield _FakeSaver()

    monkeypatch.setattr(mod, "_AsyncPostgresSaver_from_conn_string", _fake_from_conn_string)

    async with mod.agent_checkpointer("postgresql://localhost/test") as saver:
        assert saver is not None

    assert setup_called == [True], "setup() must be called before yielding"


async def test_agent_checkpointer_none_dsn_returns_none() -> None:
    """When postgres_url is None (dev without DB), yield None instead of crashing."""
    from openbot.infrastructure.persistence import agent_checkpointer as mod

    async with mod.agent_checkpointer(None) as saver:
        assert saver is None
