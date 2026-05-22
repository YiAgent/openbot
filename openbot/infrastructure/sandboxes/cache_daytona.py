"""DaytonaSnapshotCache — production sandbox cache via Daytona snapshot API.

Uses three Daytona SDK methods (all wrapped in ``asyncio.to_thread``):
  - ``client.list_snapshots(labels={"openbot_key": <key>})`` — look up.
  - ``client.create_workspace_from_snapshot(snapshot_id)`` — hydrate.
  - ``client.delete_snapshot(snapshot_id)`` — evict stale / corrupted.

The ``_refresh_to_ref`` step runs inside a ``DaytonaSandboxAdapter`` so it
reuses the same ``run()`` / ``process.exec`` pipeline as the cold-path clone.
Tokens are injected at acquire-time via ``_inject_token`` (from
``daytona.py``), never at publish-time, so snapshots never carry credentials.

Architecture notes:
  - SDK errors from ``list_snapshots`` are NOT swallowed — they propagate
    to the dispatcher which records ``backend_error`` and falls through to
    the cold path.
  - Stale / corrupted entries schedule an async eviction via
    ``_schedule_background``.  The eviction function calls
    ``client.delete_snapshot`` synchronously inside the coroutine (no
    ``asyncio.to_thread``) so a single ``await asyncio.sleep(0)`` drains
    it deterministically in tests.

Per Task 3.1 of
``docs/superpowers/plans/2026-05-21-sandbox-snapshot-cache-plan.md``
and spec § "Resolution algorithm".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from openbot.application.sandbox_cache_key import CacheCorruptedError, _cache_key
from openbot.infrastructure.sandboxes.daytona import DaytonaSandboxAdapter, _inject_token

if TYPE_CHECKING:
    from openbot.application.sandbox_handle import SandboxedHandle
    from openbot.domain.checkout import CheckoutSpec

_logger = logging.getLogger(__name__)

# GC-safe set for fire-and-forget background tasks (same pattern as dispatcher).
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _schedule_background(coro: Any) -> None:
    """Create a fire-and-forget task anchored in ``_BACKGROUND_TASKS``."""
    task: asyncio.Task[Any] = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _evict_snapshot(client: Any, snapshot_id: str) -> None:
    """Best-effort synchronous delete — called as a background task.

    Runs ``client.delete_snapshot`` synchronously inside the coroutine
    (no ``asyncio.to_thread``) so that a single ``await asyncio.sleep(0)``
    in tests is sufficient to drain the task queue deterministically.
    This is safe because evictions are rare and the coroutine is already
    fire-and-forget on the background task queue.
    """
    try:
        client.delete_snapshot(snapshot_id)
    except Exception:
        _logger.warning(
            "daytona_snapshot_evict_failed",
            extra={"snapshot_id": snapshot_id},
            exc_info=True,
        )


async def _refresh_to_ref(
    sandbox: DaytonaSandboxAdapter,
    repo_url: str,
    ref: str,
    token: str,
) -> None:
    """Bring the hydrated workspace to exactly ``ref``.

    Mirrors the three-step sequence in ``cache_fake._refresh_to_ref``
    but uses the HTTPS-only ``_inject_token`` from ``daytona.py`` —
    production workspaces always clone via HTTPS, never via ``file://``.

    Raises :class:`~openbot.application.sandbox_cache_key.CacheCorruptedError`
    when any git command returns non-zero, signalling a bad snapshot that
    should be evicted.
    """
    credentialed = _inject_token(repo_url, token)

    r1 = await sandbox.run(command=["git", "remote", "set-url", "origin", credentialed])
    if r1.exit_code != 0:
        raise CacheCorruptedError(
            f"git remote set-url failed (exit {r1.exit_code}): {r1.stderr.strip()!r}"
        )

    r2 = await sandbox.run(command=["git", "fetch", "origin", ref])
    if r2.exit_code != 0:
        raise CacheCorruptedError(f"git fetch failed (exit {r2.exit_code}): {r2.stderr.strip()!r}")

    r3 = await sandbox.run(command=["git", "reset", "--hard", ref])
    if r3.exit_code != 0:
        raise CacheCorruptedError(
            f"git reset --hard failed (exit {r3.exit_code}): {r3.stderr.strip()!r}"
        )


class DaytonaSnapshotCache:
    """Production snapshot cache: lookup → TTL check → hydrate → refresh.

    Constructor args:
      ``daytona_client`` — the Daytona SDK client (``Daytona(config)``).
      ``ttl_seconds``    — snapshot age limit; entries older than this
                           are treated as miss and evicted. Default 24 h.
      ``max_entries``    — reserved for Task 3.3 LRU eviction. Unused now.

    Implements :class:`~openbot.application.ports.sandbox_cache.SandboxCachePort`.
    """

    def __init__(
        self,
        *,
        daytona_client: Any,
        ttl_seconds: float = 86_400,
        max_entries: int = 50,
    ) -> None:
        self._client = daytona_client
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries

    # ── SandboxCachePort interface ──────────────────────────────────────── #

    async def acquire(
        self,
        checkout: CheckoutSpec,
        token: str,
        *,
        installation_id: int,
    ) -> SandboxedHandle | None:
        """Return a warm ``SandboxedHandle`` on hit, ``None`` on miss.

        SDK errors from ``list_snapshots`` propagate to the caller.
        Stale or corrupted entries return ``None`` and schedule eviction.
        """
        from openbot.application.sandbox_handle import SandboxedHandle

        key = _cache_key(checkout, installation_id=installation_id)

        # SDK error propagates → dispatcher records backend_error counter.
        snapshots: list[Any] = await asyncio.to_thread(
            self._client.list_snapshots, labels={"openbot_key": key}
        )
        if not snapshots:
            return None

        snap = snapshots[0]

        # ── TTL check ────────────────────────────────────────────────────
        created_at: datetime = snap.created_at
        if created_at.tzinfo is None:
            # Daytona SDK may return naive datetimes; assume UTC.
            created_at = created_at.replace(tzinfo=UTC)
        age_seconds = (datetime.now(UTC) - created_at).total_seconds()
        if age_seconds >= self._ttl_seconds:
            _schedule_background(_evict_snapshot(self._client, snap.id))
            return None

        # ── Hydrate ──────────────────────────────────────────────────────
        workspace = await asyncio.to_thread(self._client.create_workspace_from_snapshot, snap.id)
        adapter = DaytonaSandboxAdapter(_client=self._client, _sandbox=workspace)

        # ── Refresh to exact ref ─────────────────────────────────────────
        try:
            await _refresh_to_ref(adapter, checkout.repo_url, checkout.ref, token)
        except CacheCorruptedError:
            _schedule_background(_evict_snapshot(self._client, snap.id))
            return None

        return SandboxedHandle(sandbox=adapter, checkout=checkout, token=token)

    async def publish(
        self,
        handle: SandboxedHandle,
        *,
        installation_id: int,
    ) -> None:
        """Create a snapshot from this handle's workspace.

        Implemented in Task 3.2. Currently a no-op (idempotent stub).
        """

    async def evict_repo(self, repo_url: str, *, installation_id: int) -> None:
        """Delete all snapshots for one repo under this installation.

        Implemented in Task 3.3. Currently a no-op (idempotent stub).
        """


__all__ = ["DaytonaSnapshotCache"]
