"""NoOpSandboxCache — default ``SandboxCachePort`` when no backend is wired.

Why a class instead of leaving ``ctx.sandbox_cache`` as ``None``: the
dispatcher's ``_run_with_sandbox`` always calls ``cache.acquire(...)``
and (after cold-path success) schedules ``cache.publish(...)``. With a
nullable field every call site would need ``if cache is not None:``
guards. The null-object pattern collapses both branches into a single
code path; the ``noop`` backend always reports a miss so the cold path
always runs, which is exactly the behaviour we want when no snapshot
backend is configured.

Wired in by ``openbot/core/wiring.py`` when
``OPENBOT_SANDBOX_CACHE_BACKEND`` is unset or set to ``noop`` (per spec
``docs/superpowers/specs/2026-05-21-sandbox-snapshot-cache-design.md``
§ "Settings & DI wiring").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbot.application.sandbox_handle import SandboxedHandle
    from openbot.domain.checkout import CheckoutSpec


class NoOpSandboxCache:
    """Default cache adapter — every acquire is a miss; publish/evict no-op.

    Implements :class:`~openbot.application.ports.sandbox_cache.SandboxCachePort`
    structurally. Safe to call concurrently — no state, no I/O.
    """

    async def acquire(
        self,
        checkout: CheckoutSpec,
        token: str,
        *,
        installation_id: int,
    ) -> SandboxedHandle | None:
        """Always a miss — dispatcher falls through to the cold path."""
        return None

    async def publish(
        self,
        handle: SandboxedHandle,
        *,
        installation_id: int,
    ) -> None:
        """Discard the handle — nothing to snapshot."""

    async def evict_repo(self, repo_url: str, *, installation_id: int) -> None:
        """Nothing cached → nothing to evict."""


__all__ = ["NoOpSandboxCache"]
