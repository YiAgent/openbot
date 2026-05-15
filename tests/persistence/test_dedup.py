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

from openbot.persistence.dedup import DedupResult, WebhookDedup


@pytest.fixture
async def fake_redis() -> AsyncIterator[Any]:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


# ───── happy path ─────


async def test_first_delivery_is_fresh(fake_redis: Any) -> None:
    dedup = WebhookDedup(fake_redis)
    result = await dedup.check_and_mark("github", "deliv-1")
    assert result == DedupResult(fresh=True)
    assert bool(result) is True


async def test_repeat_delivery_is_dropped(fake_redis: Any) -> None:
    dedup = WebhookDedup(fake_redis)

    first = await dedup.check_and_mark("github", "deliv-1")
    second = await dedup.check_and_mark("github", "deliv-1")

    assert first.fresh is True
    assert second.fresh is False
    assert bool(second) is False


async def test_different_delivery_ids_are_independent(fake_redis: Any) -> None:
    dedup = WebhookDedup(fake_redis)
    a = await dedup.check_and_mark("github", "deliv-A")
    b = await dedup.check_and_mark("github", "deliv-B")
    assert a.fresh and b.fresh


async def test_different_channels_are_isolated(fake_redis: Any) -> None:
    # Same delivery_id, different channels must not collide.
    dedup = WebhookDedup(fake_redis)
    gh = await dedup.check_and_mark("github", "deliv-1")
    li = await dedup.check_and_mark("linear", "deliv-1")
    assert gh.fresh and li.fresh


# ───── TTL ─────


async def test_key_expires_after_ttl(fake_redis: Any) -> None:
    # Use a 1-second TTL so the test stays fast.
    dedup = WebhookDedup(fake_redis, ttl_seconds=1)
    first = await dedup.check_and_mark("github", "expiring")
    assert first.fresh

    # Wait past the TTL — fakeredis honors EX.
    await asyncio.sleep(1.1)

    after = await dedup.check_and_mark("github", "expiring")
    assert after.fresh, "key should have expired by now"


async def test_ttl_is_set_on_first_write(fake_redis: Any) -> None:
    dedup = WebhookDedup(fake_redis, ttl_seconds=600)
    await dedup.check_and_mark("github", "with-ttl")
    ttl = await fake_redis.ttl("openbot:dedup:webhook:github:with-ttl")
    # Allow ±1s slack for clock granularity.
    assert 598 <= ttl <= 600


# ───── degenerate inputs ─────


async def test_empty_delivery_id_is_treated_as_fresh(fake_redis: Any) -> None:
    # Parser would have logged a separate warning; dedup just lets it through.
    dedup = WebhookDedup(fake_redis)
    result = await dedup.check_and_mark("github", "")
    assert result.fresh is True
    # No key should have been created.
    keys = await fake_redis.keys("openbot:dedup:webhook:*")
    assert keys == []


# ───── fall-open behavior ─────


async def test_no_redis_returns_fresh_with_fallback_flag(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dedup = WebhookDedup(redis=None)
    with caplog.at_level(logging.WARNING, logger="openbot.persistence.dedup"):
        result = await dedup.check_and_mark("github", "deliv-1")
    assert result.fresh is True
    assert result.fallback_open is True
    assert any(r.message == "dedup_skipped_no_redis" for r in caplog.records)


async def test_redis_error_falls_open_and_logs_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = AsyncMock()
    broken.set = AsyncMock(side_effect=ConnectionError("Connection refused"))

    dedup = WebhookDedup(broken)
    with caplog.at_level(logging.ERROR, logger="openbot.persistence.dedup"):
        result = await dedup.check_and_mark("github", "deliv-1")

    assert result.fresh is True
    assert result.fallback_open is True
    records = [r for r in caplog.records if r.message == "dedup_redis_error_fail_open"]
    assert len(records) == 1
    # Logged via _logger.exception → exc_info is set.
    assert records[0].exc_info is not None


# ───── DedupResult itself ─────


def test_dedup_result_is_truthy_when_fresh() -> None:
    assert bool(DedupResult(fresh=True))
    assert not bool(DedupResult(fresh=False))


def test_dedup_result_fallback_open_default_false() -> None:
    assert DedupResult(fresh=True).fallback_open is False
