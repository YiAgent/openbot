"""Async Redis client factory.

Lifespan-managed connection pool. The webapp constructs one Redis client at
startup and shares it across all consumers (dedup, rate-limit, queue).

Production deployments MUST set `OPENBOT_REDIS_URL`. Dev `make dev` runs
without Redis — features that depend on it (currently: webhook dedup)
degrade open with a WARNING log.
"""

from __future__ import annotations

from typing import Final

import redis.asyncio as redis_async

_POOL_MAX_CONNECTIONS: Final = 20
_SOCKET_TIMEOUT_SECONDS: Final = 5.0


def make_client(url: str) -> redis_async.Redis:
    """Construct an async Redis client from a connection URL.

    `decode_responses=True` so all reads come back as `str` instead of
    `bytes` — every consumer in this codebase wants strings.
    """
    return redis_async.from_url(
        url,
        decode_responses=True,
        max_connections=_POOL_MAX_CONNECTIONS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        socket_keepalive=True,
        retry_on_timeout=True,
    )
