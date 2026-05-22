"""DaytonaSnapshotCache — unit tests for Tasks 3.1 (acquire + refresh).

All Daytona SDK calls are mocked; no network or real Daytona workspace is
created. asyncio.to_thread runs the mock callables in a thread pool, which
is fine because MagicMock is thread-safe for these synchronous calls.

SDK contract assumed by the adapter (top-level client methods):
  client.list_snapshots(labels: dict) -> list[SnapshotMeta]
    SnapshotMeta: .id: str, .labels: dict[str, str], .created_at: datetime (UTC)
  client.create_workspace_from_snapshot(snapshot_id: str) -> workspace
    workspace: .id: str, .process.exec(cmd, timeout) -> result
    result: .exit_code: int, .result: str
  client.delete_snapshot(snapshot_id: str) -> None
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from openbot.application.sandbox_cache_key import _cache_key
from openbot.domain.checkout import CheckoutSpec, CloneStrategy
from openbot.infrastructure.sandboxes.cache_daytona import DaytonaSnapshotCache

# ── Helpers ───────────────────────────────────────────────────────────────────


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _checkout(
    *,
    repo_url: str = "https://github.com/acme/widget.git",
    ref: str = "deadbeef",
    strategy: CloneStrategy = CloneStrategy.SHALLOW,
) -> CheckoutSpec:
    return CheckoutSpec(repo_url=repo_url, ref=ref, strategy=strategy)


def _mock_snapshot(
    *,
    snapshot_id: str = "snap-001",
    key: str | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    """Build a mock SnapshotMeta object."""
    snap = MagicMock()
    snap.id = snapshot_id
    snap.labels = {"openbot_key": key or "placeholder_key"}
    snap.created_at = created_at or _now_utc()
    return snap


def _mock_workspace(
    *,
    workspace_id: str = "ws-hydrated-001",
    exec_exit_code: int = 0,
) -> MagicMock:
    """Build a mock Daytona workspace (the underlying SDK object, not the adapter)."""
    ws = MagicMock()
    ws.id = workspace_id
    ws.process.exec.return_value = MagicMock(
        exit_code=exec_exit_code, result="", additional_properties={}
    )
    return ws


def _make_client(
    *,
    snapshots: list[Any] | None = None,
    workspace: MagicMock | None = None,
) -> MagicMock:
    """Build a mock Daytona client with scripted responses."""
    client = MagicMock()
    client.list_snapshots.return_value = snapshots or []
    client.create_workspace_from_snapshot.return_value = workspace or _mock_workspace()
    client.delete_snapshot.return_value = None
    return client


# ── Task 3.1 tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_miss_when_no_snapshot_matches() -> None:
    """SDK returns empty list → acquire returns None (cache miss).

    No workspace is created; no git commands run.
    """
    client = _make_client(snapshots=[])
    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)
    checkout = _checkout()

    result = await cache.acquire(checkout, "ghs_tok", installation_id=99)

    assert result is None
    client.list_snapshots.assert_called_once()
    client.create_workspace_from_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_acquire_hit_calls_hydrate_and_refresh() -> None:
    """SDK returns one matching snapshot -> adapter hydrates workspace,
    runs git refresh sequence, and returns a populated SandboxedHandle.

    Asserts:
      - create_workspace_from_snapshot called with the snapshot's id.
      - workspace.process.exec called with git remote set-url, git fetch,
        and git reset --hard commands (in that order).
      - Returned handle has the correct checkout and non-None sandbox.
    """
    checkout = _checkout()
    token = "ghs_fresh_token"
    installation_id = 42
    key = _cache_key(checkout, installation_id=installation_id)

    snap = _mock_snapshot(snapshot_id="snap-hit-001", key=key)
    ws = _mock_workspace(workspace_id="ws-hit-001")
    client = _make_client(snapshots=[snap], workspace=ws)

    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)
    handle = await cache.acquire(checkout, token, installation_id=installation_id)

    assert handle is not None
    assert handle.checkout is checkout
    assert handle.token == token

    # Workspace hydrated from the correct snapshot.
    client.create_workspace_from_snapshot.assert_called_once_with(snap.id)

    # git refresh sequence ran: set-url -> fetch -> reset
    exec_calls = ws.process.exec.call_args_list
    assert len(exec_calls) >= 3, "Expected at least 3 git commands (set-url, fetch, reset)"
    joined_cmds = " ".join(str(c) for c in exec_calls)
    assert "remote" in joined_cmds and "set-url" in joined_cmds
    assert "fetch" in joined_cmds
    assert "reset" in joined_cmds and "--hard" in joined_cmds


@pytest.mark.asyncio
async def test_acquire_treats_stale_snapshot_as_miss() -> None:
    """A snapshot older than ttl_seconds is treated as a miss.

    Asserts:
      - acquire returns None.
      - delete_snapshot is scheduled (fire-and-forget).
      - No workspace is hydrated.
    """
    checkout = _checkout()
    key = _cache_key(checkout, installation_id=99)
    # Snapshot created 25 hours ago — stale for a 24h TTL.
    old_snap = _mock_snapshot(
        snapshot_id="snap-stale-001",
        key=key,
        created_at=_now_utc() - timedelta(hours=25),
    )
    client = _make_client(snapshots=[old_snap])

    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)
    result = await cache.acquire(checkout, "ghs_t", installation_id=99)

    # Allow pending tasks (eviction) to drain.
    await asyncio.sleep(0)

    assert result is None
    client.create_workspace_from_snapshot.assert_not_called()
    # Eviction must have been called (async, but sleep(0) drains it).
    client.delete_snapshot.assert_called_once_with(old_snap.id)


@pytest.mark.asyncio
async def test_acquire_evicts_and_returns_none_on_corrupted_tree() -> None:
    """When git reset --hard returns non-zero exit, the adapter catches
    CacheCorruptedError, schedules async eviction, and returns None so
    the dispatcher falls through to the cold clone path.

    workspace.process.exec returns success for set-url and fetch but
    failure for reset --hard (simulating a corrupted working tree).
    """
    checkout = _checkout()
    key = _cache_key(checkout, installation_id=99)
    snap = _mock_snapshot(snapshot_id="snap-corrupt-001", key=key)

    # Script: first two exec calls succeed (set-url, fetch); last fails (reset).
    ws = MagicMock()
    ws.id = "ws-corrupt-001"
    ws.process.exec.side_effect = [
        MagicMock(exit_code=0, result="", additional_properties={}),  # set-url
        MagicMock(exit_code=0, result="", additional_properties={}),  # fetch
        MagicMock(exit_code=1, result="error: could not read", additional_properties={}),  # reset
    ]

    client = _make_client(snapshots=[snap], workspace=ws)
    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)
    result = await cache.acquire(checkout, "ghs_t", installation_id=99)

    await asyncio.sleep(0)  # drain eviction task

    assert result is None
    client.delete_snapshot.assert_called_once_with(snap.id)


@pytest.mark.asyncio
async def test_acquire_raises_on_sdk_backend_error() -> None:
    """When the Daytona SDK raises during list_snapshots, the exception
    propagates out of acquire so the dispatcher records backend_error.
    """
    client = MagicMock()
    client.list_snapshots.side_effect = RuntimeError("daytona connection timeout")

    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)

    with pytest.raises(RuntimeError, match="daytona connection timeout"):
        await cache.acquire(_checkout(), "ghs_t", installation_id=99)


@pytest.mark.asyncio
async def test_token_injected_via_x_access_token_scheme() -> None:
    """The token is injected into git remote set-url using the
    x-access-token URL scheme (matching DaytonaSandboxAdapter.clone).

    We verify this by inspecting the command string passed to
    workspace.process.exec for the set-url step.
    """
    checkout = _checkout()
    token = "ghs_super_secret_token"
    key = _cache_key(checkout, installation_id=99)
    snap = _mock_snapshot(snapshot_id="snap-tok-001", key=key)
    ws = _mock_workspace(workspace_id="ws-tok-001")
    client = _make_client(snapshots=[snap], workspace=ws)

    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)
    await cache.acquire(checkout, token, installation_id=99)

    exec_calls = ws.process.exec.call_args_list
    set_url_call = next(
        (c for c in exec_calls if "set-url" in str(c)),
        None,
    )
    assert set_url_call is not None, "git remote set-url exec call not found"
    assert "x-access-token:" in str(set_url_call), (
        "token should be injected via x-access-token scheme, not as raw token"
    )
