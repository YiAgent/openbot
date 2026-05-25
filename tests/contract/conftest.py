"""Contract-layer conftest.

Provides in-process substitutes for external services so contract
tests can exercise real adapter code without docker or network.

Fixtures:
  inmemory_redis       — fakeredis client (compatible with redis.asyncio)
  inmemory_db          — aiosqlite-backed SQLAlchemy session factory
  respx_mock           — respx router for mocking httpx calls

Forbidden: use-case wiring, agent runtime, real network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import respx

from openbot.testing.inmemory.postgres import build_inmemory_db
from openbot.testing.inmemory.redis import build_inmemory_redis


@pytest.fixture
async def inmemory_redis() -> AsyncIterator[object]:
    """Yield a fresh fakeredis client; flushed and closed on exit."""
    async with build_inmemory_redis() as client:
        yield client


@pytest.fixture
async def inmemory_db() -> AsyncIterator[object]:
    """Yield an aiosqlite sessionmaker with schema pre-created."""
    async with build_inmemory_db() as session_factory:
        yield session_factory


@pytest.fixture
def respx_mock() -> respx.MockRouter:
    """Yield an activated respx router for mocking httpx calls."""
    with respx.mock() as router:
        yield router
