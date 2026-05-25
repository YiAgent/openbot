"""ResourceLockPort — async-CM acquire/release."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openbot.application.ports.resource_lock import ResourceLockPort
from openbot.infrastructure.persistence.resource_lock_redis import RedisResourceLock
from openbot.testing.fakes.resource_lock import FakeResourceLock
from openbot.testing.inmemory.redis import build_inmemory_redis


@pytest.fixture(params=["fake", "real"])
async def lock(request: pytest.FixtureRequest) -> AsyncIterator[ResourceLockPort]:
    if request.param == "fake":
        yield FakeResourceLock()
    else:
        async with build_inmemory_redis() as redis:
            yield RedisResourceLock(redis=redis)


@pytest.mark.contract
class TestResourceLockContract:
    async def test_uncontended_lock_acquires(self, lock: ResourceLockPort) -> None:
        async with lock.lock("repo#1", ttl_seconds=5) as acquired:
            assert acquired is True

    async def test_contended_lock_returns_false(self, lock: ResourceLockPort) -> None:
        # FakeResourceLock uses contended_keys to simulate contention;
        # RedisResourceLock (real) detects contention via SET NX when the
        # same key is held inside a nested acquire.
        if isinstance(lock, FakeResourceLock):
            lock.contended_keys.add("repo#1")
            async with lock.lock("repo#1", ttl_seconds=5) as second:
                assert second is False
        else:
            async with lock.lock("repo#1", ttl_seconds=5):  # noqa: SIM117
                async with lock.lock("repo#1", ttl_seconds=5) as second:
                    assert second is False

    async def test_release_allows_reacquire(self, lock: ResourceLockPort) -> None:
        async with lock.lock("repo#1", ttl_seconds=5):
            pass
        async with lock.lock("repo#1", ttl_seconds=5) as acquired:
            assert acquired is True
