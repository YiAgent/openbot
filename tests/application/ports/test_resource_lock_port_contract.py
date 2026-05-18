"""Contract test — FakeResourceLock behaves like an async CM."""

from __future__ import annotations

import pytest

from tests._fakes.resource_lock import FakeResourceLock


@pytest.mark.asyncio
async def test_lock_records_acquisition() -> None:
    lock = FakeResourceLock()
    async with lock.lock("github:owner/repo:pr:1") as acquired:
        assert acquired is True
    assert lock.calls == [("github:owner/repo:pr:1", 10)]


@pytest.mark.asyncio
async def test_lock_contention_yields_false() -> None:
    lock = FakeResourceLock(acquire_result=False)
    async with lock.lock("github:owner/repo:pr:2") as acquired:
        assert acquired is False
