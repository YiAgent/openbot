"""NoOpSandboxCache — the default backend pins these invariants.

The no-op adapter is the wired-in default when ``OPENBOT_SANDBOX_CACHE_BACKEND``
is unset / ``noop``. The dispatcher must be able to call every cache
method against it without branching on ``is None``; the cold path always
runs. These tests pin exactly that contract:

  - ``acquire`` ALWAYS returns ``None`` (every call is a miss → cold
    path).
  - ``publish`` ALWAYS succeeds and is idempotent (the dispatcher does
    not await it on the critical path, so it MUST NOT raise).
  - ``evict_repo`` ALWAYS succeeds and is idempotent (called from
    webhook ingest when default-branch advances; MUST NOT raise).
  - The class satisfies ``SandboxCachePort`` at runtime so wiring
    catches an incomplete adapter at startup, not in production.

Per Task 1.3 of ``docs/superpowers/plans/2026-05-21-sandbox-snapshot-cache-plan.md``.
"""

from __future__ import annotations

import pytest

from openbot.application.ports.sandbox_cache import SandboxCachePort
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.domain.checkout import CheckoutSpec, CloneStrategy
from openbot.infrastructure.sandboxes.cache_noop import NoOpSandboxCache


def _checkout() -> CheckoutSpec:
    return CheckoutSpec(
        repo_url="https://github.com/foo/bar.git",
        ref="abc123def456abc123def456abc123def456abcd",
        strategy=CloneStrategy.SHALLOW,
    )


class _StubHandle:
    """Minimal stand-in for SandboxedHandle — publish() never inspects fields."""


@pytest.mark.asyncio
async def test_noop_acquire_returns_none() -> None:
    cache = NoOpSandboxCache()
    result = await cache.acquire(_checkout(), token="ghs_xxx", installation_id=42)
    assert result is None


@pytest.mark.asyncio
async def test_noop_publish_is_idempotent_noop() -> None:
    # Publish is fire-and-forget from the dispatcher's view. The no-op
    # MUST NOT raise (no exception channel back to the dispatcher) and
    # MUST be safe to call multiple times with the same handle.
    cache = NoOpSandboxCache()
    handle: SandboxedHandle = _StubHandle()  # type: ignore[assignment]
    await cache.publish(handle, installation_id=42)
    await cache.publish(handle, installation_id=42)


@pytest.mark.asyncio
async def test_noop_evict_repo_is_idempotent_noop() -> None:
    # Webhook ingest calls this when default branch advances. No-op
    # backend has nothing to evict; second call must still be a no-op.
    cache = NoOpSandboxCache()
    await cache.evict_repo("https://github.com/foo/bar.git", installation_id=42)
    await cache.evict_repo("https://github.com/foo/bar.git", installation_id=42)


def test_noop_satisfies_port() -> None:
    # @runtime_checkable Protocol — wiring uses isinstance(adapter, Port)
    # at startup to reject incomplete adapters. The default backend MUST
    # satisfy that check or the app fails to boot.
    assert isinstance(NoOpSandboxCache(), SandboxCachePort)
