"""Redis-backed ResourceLockPort."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

import redis.asyncio as redis_async

from openbot.application.state.resource_lock import resource_lock

if TYPE_CHECKING:
    from openbot.application.ports.resource_lock import ResourceLockPort


class RedisResourceLock:
    """Wraps the free resource_lock() context manager with a stateful API."""

    def __init__(self, redis: redis_async.Redis | None) -> None:
        self._redis = redis

    def lock(
        self, resource_key: str, *, ttl_seconds: int = 10
    ) -> AbstractAsyncContextManager[bool]:
        return resource_lock(self._redis, resource_key, ttl_seconds=ttl_seconds)


if TYPE_CHECKING:
    _witness: ResourceLockPort = RedisResourceLock(redis=None)
