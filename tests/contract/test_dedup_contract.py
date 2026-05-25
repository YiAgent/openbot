"""DedupPort contract — idempotent FRESH/DUPLICATE/FALLBACK_OPEN."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openbot.application.ports.dedup import DedupPort
from openbot.domain.dedup import DedupOutcome
from openbot.infrastructure.persistence.dedup import WebhookDedup
from openbot.testing.fakes.dedup import FakeDedup
from openbot.testing.inmemory.redis import build_inmemory_redis


@pytest.fixture(params=["fake", "real"])
async def dedup(request: pytest.FixtureRequest) -> AsyncIterator[DedupPort]:
    if request.param == "fake":
        yield FakeDedup()
    else:
        async with build_inmemory_redis() as redis:
            yield WebhookDedup(redis=redis, ttl_seconds=60)


@pytest.mark.contract
class TestDedupContract:
    async def test_first_call_is_fresh(self, dedup: DedupPort) -> None:
        outcome = await dedup.check_and_mark("github", "delivery-1")
        assert outcome is DedupOutcome.FRESH

    async def test_replay_is_duplicate(self, dedup: DedupPort) -> None:
        await dedup.check_and_mark("github", "delivery-1")
        outcome = await dedup.check_and_mark("github", "delivery-1")
        assert outcome is DedupOutcome.DUPLICATE

    async def test_distinct_channels_are_independent(self, dedup: DedupPort) -> None:
        await dedup.check_and_mark("github", "x")
        outcome = await dedup.check_and_mark("slack", "x")
        assert outcome is DedupOutcome.FRESH
