"""Contract test — FakeRateLimiter enforces limit."""

from __future__ import annotations

import pytest

from tests._fakes.rate_limiter import FakeRateLimiter


@pytest.mark.asyncio
async def test_allow_under_limit_deny_over() -> None:
    rl = FakeRateLimiter()
    assert await rl.check("k", limit=2, window_seconds=60) is True
    assert await rl.check("k", limit=2, window_seconds=60) is True
    assert await rl.check("k", limit=2, window_seconds=60) is False
    assert rl.counts == {"k": 3}
