"""InMemorySandboxCache — process-local LRU + TTL warm pool.

Used for **tests and dev** in place of ``DaytonaSnapshotCache``:

  - Tests don't pay cold-start cost (no network clone per test run).
  - Dev mode warms up the pool from real clones; subsequent requests
    in the same process hit the warm pool.

This adapter intentionally holds **live sandbox objects**, not serialised
snapshots. The consequence is that the pool survives only for the
process lifetime — a worker restart is a full cache miss. Production
workloads that need cross-restart persistence must use
``DaytonaSnapshotCache`` (Part 3).

Architecture:
  - ``_index`` is a ``collections.OrderedDict`` keyed by
    ``_cache_key(checkout, installation_id=...)``. Insertion order is
    preserved; ``move_to_end`` implements LRU recency bumping in O(1).
  - ``_lock`` is an ``asyncio.Lock`` that protects all mutations to
    ``_index``. Git I/O (``_refresh_to_ref``) runs **outside** the lock
    to avoid serialising concurrent acquires.
  - ``size()`` is a test-visible convenience; production code uses the
    Port interface only.

Per Task 1.4 of
``docs/superpowers/plans/2026-05-21-sandbox-snapshot-cache-plan.md``
and § "Resolution algorithm" of the predecessor spec.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openbot.application.sandbox_cache_key import CacheCorruptedError, _cache_key

# Same pattern as daytona._redact_tokens — strip x-access-token credentials
# from git error messages before embedding them in exception text or logs.
_TOKEN_PATTERN = re.compile(r"x-access-token:[^@\s]+@")


def _redact_tokens(text: str) -> str:
    return _TOKEN_PATTERN.sub("x-access-token:<redacted>@", text)


if TYPE_CHECKING:
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.application.sandbox_handle import SandboxedHandle
    from openbot.domain.checkout import CheckoutSpec


@dataclass
class _Entry:
    """One cached warm sandbox."""

    sandbox: SandboxPort
    checkout: CheckoutSpec
    installation_id: int
    created_at: float = field(default_factory=time.monotonic)
    last_access: float = field(default_factory=time.monotonic)


def _inject_token(repo_url: str, token: str) -> str:
    """Build the credentialed origin URL for the fetch step.

    HTTPS repos get ``x-access-token:<token>@`` injected; non-HTTPS
    (``file://`` in tests) are returned unchanged — no credential needed.
    Consistent with ``DaytonaSandboxAdapter._inject_token`` but without
    the ValueError for non-HTTPS, because tests legitimately use
    ``file://`` origins.
    """
    if repo_url.startswith("https://"):
        return repo_url.replace("https://", f"https://x-access-token:{token}@", 1)
    return repo_url


async def _refresh_to_ref(sandbox: SandboxPort, repo_url: str, ref: str, token: str) -> None:
    """Bring the sandbox workspace to exactly ``ref``.

    Three-step sequence mandated by the spec:

    1. ``git remote set-url origin <credentialed-url>`` — refresh the
       token in the origin URL. Installation tokens have a ~1 h TTL;
       the token present at publish-time is almost certainly stale by
       the time we acquire. We must inject the fresh ``token`` from the
       ``acquire`` call before fetching.

    2. ``git fetch origin <ref>`` — pull the exact SHA we need.

    3. ``git reset --hard <ref>`` — advance the working tree to that
       SHA. Non-zero exit signals a corrupted workspace; raise
       :class:`~openbot.application.sandbox_cache_key.CacheCorruptedError`
       so the caller can evict and fall through to the cold path.
    """
    credentialed = _inject_token(repo_url, token)

    r1 = await sandbox.run(command=["git", "remote", "set-url", "origin", credentialed])
    if r1.exit_code != 0:
        raise CacheCorruptedError(
            f"git remote set-url failed (exit {r1.exit_code}): {_redact_tokens(r1.stderr.strip())!r}"
        )

    r2 = await sandbox.run(command=["git", "fetch", "origin", ref])
    if r2.exit_code != 0:
        raise CacheCorruptedError(
            f"git fetch failed (exit {r2.exit_code}): {_redact_tokens(r2.stderr.strip())!r}"
        )

    r3 = await sandbox.run(command=["git", "reset", "--hard", ref])
    if r3.exit_code != 0:
        raise CacheCorruptedError(
            f"git reset --hard failed (exit {r3.exit_code}): {_redact_tokens(r3.stderr.strip())!r}"
        )


class InMemorySandboxCache:
    """LRU + TTL in-process sandbox cache.

    Constructor:

      ``max_entries`` — maximum number of live sandboxes to keep.
        When exceeded, the least-recently-used entry is evicted (the
        first entry in insertion order after all recency bumps).

      ``ttl_seconds`` — entries older than this many seconds are treated
        as miss on the next ``acquire`` and pruned. Set to 0 to make
        every entry immediately stale (useful in tests).

    Thread-safety: all mutations are guarded by ``_lock``. Safe for
    concurrent ``asyncio`` workers — NOT safe across OS threads.
    """

    def __init__(self, *, max_entries: int = 50, ttl_seconds: float = 86_400) -> None:
        self._index: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds

    # ------------------------------------------------------------------ #
    # SandboxCachePort interface                                           #
    # ------------------------------------------------------------------ #

    async def acquire(
        self,
        checkout: CheckoutSpec,
        token: str,
        *,
        installation_id: int,
    ) -> SandboxedHandle | None:
        """Return a warm handle on hit, ``None`` on miss / stale / error."""
        from openbot.application.sandbox_handle import SandboxedHandle

        key = _cache_key(checkout, installation_id=installation_id)

        async with self._lock:
            if key not in self._index:
                return None

            entry = self._index[key]
            now = time.monotonic()

            if now - entry.created_at >= self._ttl_seconds:
                # Stale — prune on access rather than keeping cruft.
                del self._index[key]
                return None

            # Recency bump: move to the back so eviction hits older entries.
            self._index.move_to_end(key)
            entry.last_access = now
            sandbox = entry.sandbox

        # Run the refresh OUTSIDE the lock — it's I/O and may take seconds.
        try:
            await _refresh_to_ref(sandbox, checkout.repo_url, checkout.ref, token)
        except CacheCorruptedError:
            async with self._lock:
                self._index.pop(key, None)
            return None

        return SandboxedHandle(sandbox=sandbox, checkout=checkout, token=token)

    async def publish(
        self,
        handle: SandboxedHandle,
        *,
        installation_id: int,
    ) -> None:
        """Insert the handle if no entry already exists for this key.

        Idempotent: a second ``publish`` for the same
        ``(installation_id, checkout)`` is a no-op — the first snapshot
        wins. This prevents two workers that both finish cold-path at
        the same millisecond from producing duplicate entries.
        """
        key = _cache_key(handle.checkout, installation_id=installation_id)

        async with self._lock:
            if key in self._index:
                return

            if len(self._index) >= self._max_entries:
                # Drop the LRU entry (first in insertion order after recency
                # bumps) to make room for the new one.
                self._index.popitem(last=False)

            now = time.monotonic()
            self._index[key] = _Entry(
                sandbox=handle.sandbox,
                checkout=handle.checkout,
                installation_id=installation_id,
                created_at=now,
                last_access=now,
            )

    async def evict_repo(self, repo_url: str, *, installation_id: int) -> None:
        """Remove every cached entry for ``(installation_id, repo_url)``.

        Called by webhook ingest when the default branch advances by
        enough commits that all cached refs for a repo are presumed stale.
        """
        normalized = repo_url.rstrip("/").lower()

        async with self._lock:
            to_remove = [
                k
                for k, entry in self._index.items()
                if (
                    entry.installation_id == installation_id
                    and entry.checkout.repo_url.rstrip("/").lower() == normalized
                )
            ]
            for k in to_remove:
                del self._index[k]

    # ------------------------------------------------------------------ #
    # Test / introspection helpers                                         #
    # ------------------------------------------------------------------ #

    def size(self) -> int:
        """Return the current number of cached entries.

        Deliberately NOT part of ``SandboxCachePort`` — it exists only
        so test assertions can read the index without inspecting private
        state directly.
        """
        return len(self._index)


__all__ = ["InMemorySandboxCache"]
