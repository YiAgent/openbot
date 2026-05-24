"""Contract test — SandboxCachePort runtime-checkability and shape.

The port is the dispatcher's contract for "is there a cached sandbox I
can serve instead of paying cold-start?". Adapters implement it; the
dispatcher checks ``isinstance(adapter, SandboxCachePort)`` at wiring
time. These tests pin both invariants:

  1. A class that implements all three coroutines satisfies the
     Protocol at runtime (``@runtime_checkable``).
  2. A class missing any one of ``acquire`` / ``publish`` /
     ``evict_repo`` does NOT satisfy the Protocol — so wiring catches
     incomplete adapters before they reach production.

Per Task 1.1 of ``docs/superpowers/plans/2026-05-21-sandbox-snapshot-cache-plan.md``.
NoOp / InMemory / Daytona adapter contract coverage lives in their
own test files (Tasks 1.3, 1.4, and Part 3 respectively).
"""

from __future__ import annotations

from openbot.application.ports.sandbox_cache import SandboxCachePort
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.domain.checkout import CheckoutSpec


class _Complete:
    """Minimal valid implementer — exercises the happy isinstance path."""

    async def acquire(
        self,
        checkout: CheckoutSpec,
        token: str,
        *,
        installation_id: int,
    ) -> SandboxedHandle | None:
        return None

    async def publish(
        self,
        handle: SandboxedHandle,
        *,
        installation_id: int,
    ) -> None:
        return None

    async def evict_repo(self, repo_url: str, *, installation_id: int) -> None:
        return None


class _MissingAcquire:
    async def publish(
        self,
        handle: SandboxedHandle,
        *,
        installation_id: int,
    ) -> None:
        return None

    async def evict_repo(self, repo_url: str, *, installation_id: int) -> None:
        return None


class _MissingPublish:
    async def acquire(
        self,
        checkout: CheckoutSpec,
        token: str,
        *,
        installation_id: int,
    ) -> SandboxedHandle | None:
        return None

    async def evict_repo(self, repo_url: str, *, installation_id: int) -> None:
        return None


class _MissingEvictRepo:
    async def acquire(
        self,
        checkout: CheckoutSpec,
        token: str,
        *,
        installation_id: int,
    ) -> SandboxedHandle | None:
        return None

    async def publish(
        self,
        handle: SandboxedHandle,
        *,
        installation_id: int,
    ) -> None:
        return None


def test_complete_implementer_satisfies_port() -> None:
    assert isinstance(_Complete(), SandboxCachePort)


def test_missing_acquire_does_not_satisfy_port() -> None:
    assert not isinstance(_MissingAcquire(), SandboxCachePort)


def test_missing_publish_does_not_satisfy_port() -> None:
    assert not isinstance(_MissingPublish(), SandboxCachePort)


def test_missing_evict_repo_does_not_satisfy_port() -> None:
    assert not isinstance(_MissingEvictRepo(), SandboxCachePort)
