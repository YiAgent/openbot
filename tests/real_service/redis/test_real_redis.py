"""Real-service tests for Redis-backed adapters.

Skipped automatically when OPENBOT_REDIS_URL is absent.
Requires a real Redis instance — not fakeredis.

These tests verify that the real adapter implementations work correctly
against live Redis (not just against the in-memory substitute).
"""

from __future__ import annotations

import uuid

import pytest

from tests.real_service.conftest import _env_or_skip

# Module-level skip: one 's' per file, not one per test
env = _env_or_skip("OPENBOT_REDIS_URL")


@pytest.mark.real_service
class TestRealRedisDedup:
    async def test_fresh_delivery_is_fresh(self) -> None:
        """FRESH on first check_and_mark with a real Redis store."""
        from openbot.infrastructure.persistence.dedup_redis import WebhookDedup

        import redis.asyncio as redis_async
        from openbot.domain.dedup import DedupOutcome

        client = redis_async.from_url(env["OPENBOT_REDIS_URL"])
        try:
            dedup = WebhookDedup(redis=client)
            delivery_id = f"rs-test-{uuid.uuid4().hex}"
            outcome = await dedup.check_and_mark("github", delivery_id)
            assert outcome is DedupOutcome.FRESH
        finally:
            await client.aclose()

    async def test_duplicate_delivery_is_duplicate(self) -> None:
        """DUPLICATE on second check_and_mark with a real Redis store."""
        from openbot.infrastructure.persistence.dedup_redis import WebhookDedup

        import redis.asyncio as redis_async
        from openbot.domain.dedup import DedupOutcome

        client = redis_async.from_url(env["OPENBOT_REDIS_URL"])
        try:
            dedup = WebhookDedup(redis=client)
            delivery_id = f"rs-test-{uuid.uuid4().hex}"
            await dedup.check_and_mark("github", delivery_id)
            outcome = await dedup.check_and_mark("github", delivery_id)
            assert outcome is DedupOutcome.DUPLICATE
        finally:
            await client.aclose()

    async def test_cancellation_signal_persists(self) -> None:
        """Signal then check returns True with a real Redis store."""
        import redis.asyncio as redis_async
        from openbot.infrastructure.persistence.cancellation_redis import RedisCancellation

        client = redis_async.from_url(env["OPENBOT_REDIS_URL"])
        try:
            cancellation = RedisCancellation(redis=client)
            run_id = f"rs-run-{uuid.uuid4().hex}"
            await cancellation.signal(run_id)
            result = await cancellation.is_cancelled(run_id)
            assert result is True
        finally:
            await client.aclose()
