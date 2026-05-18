"""Redis-backed CancellationPort."""

from __future__ import annotations

from typing import TYPE_CHECKING

import redis.asyncio as redis_async

from openbot.application.state.cancellation import is_cancelled, signal

if TYPE_CHECKING:
    from openbot.application.ports.cancellation import CancellationPort


class RedisCancellation:
    """Wraps the free signal() / is_cancelled() functions with a stateful API."""

    def __init__(self, redis: redis_async.Redis | None) -> None:
        self._redis = redis

    async def signal(self, run_id: str) -> None:
        await signal(self._redis, run_id)

    async def is_cancelled(self, run_id: str) -> bool:
        return await is_cancelled(self._redis, run_id)


if TYPE_CHECKING:
    _witness: CancellationPort = RedisCancellation(redis=None)
