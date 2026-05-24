"""In-memory Redis client for contract / integration / e2e tests.

Uses fakeredis.aioredis which mirrors redis.asyncio.Redis surface area.
Production adapters (RedisQueue, RedisDedup, etc.) cannot tell the
difference; this is what makes the contract layer's "real" path
exercise real adapter code without docker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import fakeredis.aioredis


@asynccontextmanager
async def build_inmemory_redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    """Yield a fresh fakeredis client; flush + close on exit."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


__all__ = ["build_inmemory_redis"]
