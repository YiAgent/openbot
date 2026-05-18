"""RateLimiterPort — INCR-based allow/deny."""

from __future__ import annotations

from typing import Protocol


class RateLimiterPort(Protocol):
    """Count-based rate limiter keyed by an arbitrary string.

    Returns True if the request is allowed (count after increment ≤ limit);
    False if it exceeded the limit. Fail-open semantics on backend error.
    """

    async def check(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> bool: ...


__all__ = ["RateLimiterPort"]
