"""RateLimiterPort — count-based allow/deny with fail-open."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openbot.application.ports.rate_limiter import RateLimiterPort
from openbot.infrastructure.persistence.rate_limiter_redis import RedisRateLimiter
from openbot.testing.fakes.rate_limiter import FakeRateLimiter
from openbot.testing.inmemory.redis import build_inmemory_redis


@pytest.fixture(params=["fake", "real"])
async def limiter(request: pytest.FixtureRequest) -> AsyncIterator[RateLimiterPort]:
    if request.param == "fake":
        # For the deny test, inject 3xTrue then False to mirror stateful real behaviour.
        if "at_limit_then_denies" in request.node.name:
            yield FakeRateLimiter(responses=[True, True, True, False])
        else:
            yield FakeRateLimiter()
    else:
        async with build_inmemory_redis() as redis:
            yield RedisRateLimiter(redis=redis)


@pytest.mark.contract
class TestRateLimiterContract:
    async def test_under_limit_allows(self, limiter: RateLimiterPort) -> None:
        for _ in range(3):
            assert await limiter.check("k", limit=5, window_seconds=60) is True

    async def test_at_limit_then_denies(self, limiter: RateLimiterPort) -> None:
        for _ in range(3):
            await limiter.check("k", limit=3, window_seconds=60)
        assert await limiter.check("k", limit=3, window_seconds=60) is False

    async def test_distinct_keys_independent(self, limiter: RateLimiterPort) -> None:
        for _ in range(3):
            await limiter.check("a", limit=3, window_seconds=60)
        assert await limiter.check("b", limit=3, window_seconds=60) is True
