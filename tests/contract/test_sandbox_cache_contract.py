"""Contract tests for SandboxCachePort.

[fake] uses FakeSandboxCache (response-queue), [real] uses InMemorySandboxCache.

The in-process "real" implementation is InMemorySandboxCache from
openbot/infrastructure/sandboxes/cache_fake.py (an in-memory LRU cache,
not backed by Daytona). The Daytona-backed cache has no in-process
substitute and is covered by the real_service layer.
"""

from __future__ import annotations

import pytest

from openbot.application.ports.sandbox_cache import SandboxCachePort
from openbot.domain.checkout import CheckoutSpec
from openbot.infrastructure.sandboxes.cache_fake import InMemorySandboxCache
from openbot.testing.fakes import FakeSandboxCache


def _checkout() -> CheckoutSpec:
    return CheckoutSpec(
        repo_url="https://github.com/owner/repo.git",
        ref="abc" * 13 + "a",  # 40-char hex SHA
    )


@pytest.fixture(params=["fake", "real"])
def sandbox_cache(request: pytest.FixtureRequest) -> SandboxCachePort:
    if request.param == "fake":
        return FakeSandboxCache()
    else:
        return InMemorySandboxCache()


@pytest.mark.contract
class TestSandboxCacheContract:
    async def test_acquire_miss_returns_none(self, sandbox_cache: SandboxCachePort) -> None:
        result = await sandbox_cache.acquire(
            _checkout(),
            "fake-token",
            installation_id=1,
        )
        assert result is None

    async def test_evict_does_not_raise(self, sandbox_cache: SandboxCachePort) -> None:
        """evict_repo must be a no-op when the repo is not cached."""
        await sandbox_cache.evict_repo("https://github.com/owner/repo.git", installation_id=1)
