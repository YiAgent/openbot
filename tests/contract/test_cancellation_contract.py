"""CancellationPort — durable cancel signal."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openbot.application.ports.cancellation import CancellationPort
from openbot.infrastructure.persistence.cancellation_redis import RedisCancellation
from openbot.testing.fakes.cancellation import FakeCancellation
from openbot.testing.inmemory.redis import build_inmemory_redis


@pytest.fixture(params=["fake", "real"])
async def cancellation(request: pytest.FixtureRequest) -> AsyncIterator[CancellationPort]:
    if request.param == "fake":
        yield FakeCancellation()
    else:
        async with build_inmemory_redis() as redis:
            yield RedisCancellation(redis=redis)


@pytest.mark.contract
class TestCancellationContract:
    async def test_unsignaled_returns_false(self, cancellation: CancellationPort) -> None:
        assert await cancellation.is_cancelled("r1") is False

    async def test_signal_then_check_returns_true(self, cancellation: CancellationPort) -> None:
        await cancellation.signal("r1")
        assert await cancellation.is_cancelled("r1") is True

    async def test_distinct_run_ids_are_independent(self, cancellation: CancellationPort) -> None:
        await cancellation.signal("r1")
        assert await cancellation.is_cancelled("r2") is False
