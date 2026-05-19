"""Redis-backed RateLimiterPort using INCR + EXPIRE pipeline."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis.asyncio as redis_async

    from openbot.application.ports.rate_limiter import RateLimiterPort

_logger = logging.getLogger(__name__)


class RedisRateLimiter:
    """RateLimiterPort impl backed by Redis INCR + EXPIRE.

    Uses the same INCR+EXPIRE pipeline as the legacy _incr_with_ttl helper,
    encapsulating the Redis dependency behind the Port interface.
    Fail-open: returns True when Redis is unavailable so a Redis flap
    never blocks all webhooks (consistent with WebhookDedup semantics).
    """

    def __init__(self, redis: redis_async.Redis | None) -> None:
        self._redis = redis

    async def check(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> bool:
        """Return True if allowed (count ≤ limit), False if rate-limited."""
        if self._redis is None:
            return True  # fail-open: no Redis = no rate-limit

        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.incr(key)
            pipe.expire(key, window_seconds)
            results = await pipe.execute()
            count = int(results[0])
            return count <= limit
        except Exception:
            _logger.exception(
                "rate_limiter_redis_error_fail_open",
                extra={"key": key},
            )
            return True  # fail-open


if TYPE_CHECKING:
    _witness: RateLimiterPort = RedisRateLimiter(redis=None)


__all__ = ["RedisRateLimiter"]
