"""WebhookDedup — atomic SETNX-based webhook dedup.

Uses `fakeredis.aioredis.FakeRedis` so unit tests don't need a running Redis.
The fakeredis API is contract-compatible with `redis.asyncio.Redis` for the
subset we exercise (set NX EX, get, ttl).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from openbot.persistence.dedup import DedupOutcome, WebhookDedup


@pytest.fixture
async def fake_redis() -> AsyncIterator[Any]:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


# ───── happy path ─────


async def test_first_delivery_is_fresh(fake_redis: Any) -> None:
    dedup = WebhookDedup(fake_redis)
    assert await dedup.check_and_mark("github", "deliv-1") is DedupOutcome.FRESH


async def test_repeat_delivery_is_duplicate(fake_redis: Any) -> None:
    dedup = WebhookDedup(fake_redis)

    first = await dedup.check_and_mark("github", "deliv-1")
    second = await dedup.check_and_mark("github", "deliv-1")

    assert first is DedupOutcome.FRESH
    assert second is DedupOutcome.DUPLICATE


async def test_different_delivery_ids_are_independent(fake_redis: Any) -> None:
    dedup = WebhookDedup(fake_redis)
    a = await dedup.check_and_mark("github", "deliv-A")
    b = await dedup.check_and_mark("github", "deliv-B")
    assert a is DedupOutcome.FRESH and b is DedupOutcome.FRESH


async def test_different_channels_are_isolated(fake_redis: Any) -> None:
    # Same delivery_id, different channels must not collide.
    dedup = WebhookDedup(fake_redis)
    gh = await dedup.check_and_mark("github", "deliv-1")
    li = await dedup.check_and_mark("linear", "deliv-1")
    assert gh is DedupOutcome.FRESH and li is DedupOutcome.FRESH


# ───── concurrency ─────


async def test_concurrent_check_and_mark_yields_exactly_one_fresh(
    fake_redis: Any,
) -> None:
    """The whole point of SET NX EX is single-winner semantics under contention.

    Two coroutines awaiting the same (channel, delivery_id) MUST resolve to
    exactly one FRESH (the winner) and one DUPLICATE (the loser). If someone
    later "optimizes" the implementation to GET-then-SET, this test catches
    the regression — both calls would return FRESH and we'd double-process.
    """
    dedup = WebhookDedup(fake_redis)

    results = await asyncio.gather(
        dedup.check_and_mark("github", "race"),
        dedup.check_and_mark("github", "race"),
        dedup.check_and_mark("github", "race"),
    )
    fresh_count = results.count(DedupOutcome.FRESH)
    duplicate_count = results.count(DedupOutcome.DUPLICATE)
    assert fresh_count == 1
    assert duplicate_count == 2


# ───── TTL ─────


async def test_key_expires_after_ttl(fake_redis: Any) -> None:
    # Use a 1-second TTL so the test stays fast.
    dedup = WebhookDedup(fake_redis, ttl_seconds=1)
    assert await dedup.check_and_mark("github", "expiring") is DedupOutcome.FRESH

    # Wait past the TTL — fakeredis honors EX.
    await asyncio.sleep(1.1)

    outcome_after_ttl = await dedup.check_and_mark("github", "expiring")
    assert outcome_after_ttl is DedupOutcome.FRESH, "key should have expired by now"


async def test_ttl_is_set_on_first_write(fake_redis: Any) -> None:
    dedup = WebhookDedup(fake_redis, ttl_seconds=600)
    await dedup.check_and_mark("github", "with-ttl")
    ttl = await fake_redis.ttl("openbot:dedup:webhook:github:with-ttl")
    # Allow ±1s slack for clock granularity.
    assert 598 <= ttl <= 600


# ───── degenerate inputs ─────


async def test_empty_delivery_id_returns_fresh_without_writing(
    fake_redis: Any,
) -> None:
    # Parser would have logged a separate warning; dedup just lets it through.
    dedup = WebhookDedup(fake_redis)
    outcome = await dedup.check_and_mark("github", "")
    assert outcome is DedupOutcome.FRESH
    # No key should have been created.
    keys = await fake_redis.keys("openbot:dedup:webhook:*")
    assert keys == []


# ───── fall-open behavior ─────


async def test_no_redis_returns_fallback_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dedup = WebhookDedup(redis=None)
    with caplog.at_level(logging.WARNING, logger="openbot.persistence.dedup"):
        outcome = await dedup.check_and_mark("github", "deliv-1")
    assert outcome is DedupOutcome.FALLBACK_OPEN
    assert any(r.message == "dedup_skipped_no_redis" for r in caplog.records)


async def test_redis_error_falls_open_and_logs_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = AsyncMock()
    broken.set = AsyncMock(side_effect=ConnectionError("Connection refused"))

    dedup = WebhookDedup(broken)
    with caplog.at_level(logging.ERROR, logger="openbot.persistence.dedup"):
        outcome = await dedup.check_and_mark("github", "deliv-1")

    assert outcome is DedupOutcome.FALLBACK_OPEN
    records = [r for r in caplog.records if r.message == "dedup_redis_error_fail_open"]
    assert len(records) == 1
    # Logged via _logger.exception → exc_info is set.
    assert records[0].exc_info is not None


# ───── DedupOutcome.should_process ─────


def test_fresh_should_process() -> None:
    assert DedupOutcome.FRESH.should_process is True


def test_fallback_open_should_process() -> None:
    """Fall-open MUST proceed — the whole point of the trade-off."""
    assert DedupOutcome.FALLBACK_OPEN.should_process is True


def test_duplicate_should_not_process() -> None:
    assert DedupOutcome.DUPLICATE.should_process is False
