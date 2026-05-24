"""SandboxCachePort — optional warm-sandbox cache between dispatcher and factory.

The dispatcher's ``_run_with_sandbox`` (see ``application/dispatcher.py``)
consults this port BEFORE opening a fresh sandbox via
``ctx.sandbox_factory``. On hit, the handler is invoked against the
hydrated sandbox in < 1 s; on miss, the existing cold path runs and the
new workspace is published back asynchronously.

The port is intentionally narrow:

  - ``acquire`` returns ``None`` on miss; backend errors MAY raise but
    the dispatcher catches them and treats them as miss (the cache is
    an optimization, never a correctness boundary).
  - ``publish`` is idempotent: repeated calls with the same key MUST
    NOT create duplicate cache entries. Implementations may schedule
    snapshot creation asynchronously — the dispatcher does not await
    publish on the critical path.
  - ``evict_repo`` is called by the webhook ingest layer when the
    default branch advances far enough that cached refs are likely
    stale (per spec § "Eviction → repo-level invalidation").

Adapters live under ``infrastructure/sandboxes/``:
  - ``cache_noop.py`` — default when no snapshot backend is configured.
  - ``cache_fake.py`` — in-memory LRU+TTL warm pool (tests + dev).
  - ``cache_daytona.py`` — production snapshot cache via Daytona SDK.

Per spec ``docs/superpowers/specs/2026-05-21-sandbox-snapshot-cache-design.md``
§ "Type contract".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from openbot.application.sandbox_handle import SandboxedHandle
    from openbot.domain.checkout import CheckoutSpec


@runtime_checkable
class SandboxCachePort(Protocol):
    """Optional cache layer for sandbox provisioning.

    Implementations MUST be safe to call concurrently (multiple workers
    may try to acquire / publish the same key in the same second).
    Implementations MUST NOT raise on cache misses — return ``None``.
    Backend errors (Daytona timeout, IO failure) MAY raise, in which
    case the dispatcher logs and falls through to the cold path.
    """

    async def acquire(
        self,
        checkout: CheckoutSpec,
        token: str,
        *,
        installation_id: int,
    ) -> SandboxedHandle | None:
        """Try to hydrate a SandboxedHandle from a stored snapshot.

        Returns ``None`` on miss (no snapshot, or expired by policy).

        On hit, the returned handle's ``sandbox.workspace`` MUST be at
        exactly ``checkout.ref`` — implementations are required to run
        a fast ``git fetch && git reset --hard {ref}`` between hydrate
        and return so the working tree state is deterministic
        regardless of when the snapshot was created. A post-hydrate
        ``git reset`` failure signals a corrupted snapshot and should
        cause the adapter to evict the entry and return ``None`` (the
        dispatcher then falls through to the cold path).
        """
        ...

    async def publish(
        self,
        handle: SandboxedHandle,
        *,
        installation_id: int,
    ) -> None:
        """Record this handle as cacheable.

        May snapshot synchronously or schedule async work.

        Idempotent: publishing the same ``(installation_id, checkout)``
        twice MUST NOT create two entries. Implementations should
        look up the key first and skip the snapshot call when an entry
        already exists.

        Implementations MUST NOT persist ``handle.token`` — tokens have
        ~1 h lifetime and snapshots have ~24 h lifetime; storing the
        token in the snapshot would extend its blast radius beyond the
        installation token's intended scope.
        """
        ...

    async def evict_repo(self, repo_url: str, *, installation_id: int) -> None:
        """Invalidate every snapshot for ``repo_url`` under this installation.

        Called by webhook ingest when the default branch advances by
        more than ``OPENBOT_SANDBOX_CACHE_INVALIDATION_COMMITS``
        (default 100) commits past the most-recently-cached SHA — at
        which point all cached refs are presumed stale and the storage
        is better used by other repos.

        Idempotent: a no-op if no entries match.
        """
        ...


__all__ = ["SandboxCachePort"]
