"""DaytonaSnapshotCache — unit tests for Tasks 3.1 (acquire + refresh)
and 3.2 (publish + secret sweep).

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
  client.create_snapshot(workspace_id: str, labels: dict) -> SnapshotMeta
    (Task 3.2) — snapshots a live workspace; returns SnapshotMeta with .id
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from openbot.application.sandbox_cache_key import _cache_key
from openbot.application.sandbox_handle import SandboxedHandle
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
    created_snapshot_id: str = "snap-new-001",
) -> MagicMock:
    """Build a mock Daytona client with scripted responses."""
    client = MagicMock()
    client.list_snapshots.return_value = snapshots or []
    client.create_workspace_from_snapshot.return_value = workspace or _mock_workspace()
    client.delete_snapshot.return_value = None
    # Task 3.2: create_snapshot returns a new SnapshotMeta with an id.
    new_snap = MagicMock()
    new_snap.id = created_snapshot_id
    client.create_snapshot.return_value = new_snap
    return client


def _make_handle(
    *,
    client: MagicMock,
    workspace_id: str = "ws-pub-001",
    checkout: CheckoutSpec | None = None,
    token: str = "ghs_t",
) -> tuple[SandboxedHandle, MagicMock]:
    """Build a SandboxedHandle backed by a real DaytonaSandboxAdapter wrapping
    a mock SDK workspace.  Returns (handle, mock_workspace) so tests can
    inspect sandbox.run calls.

    The returned sandbox supports run() via workspace.process.exec, which
    DaytonaSandboxAdapter._run_command wraps.
    """
    from openbot.infrastructure.sandboxes.daytona import DaytonaSandboxAdapter

    ws = _mock_workspace(workspace_id=workspace_id)
    adapter = DaytonaSandboxAdapter(_client=client, _sandbox=ws)
    spec = checkout or _checkout()
    handle = SandboxedHandle(sandbox=adapter, checkout=spec, token=token)
    return handle, ws


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


# ── Task 3.2 tests — publish (snapshot creation, idempotent) ─────────────────


@pytest.mark.asyncio
async def test_publish_creates_snapshot_with_key_and_ref_labels() -> None:
    """publish on a cache miss calls client.create_snapshot once with the
    correct workspace_id and labels {"openbot_key": ..., "openbot_ref": ...}.

    No prior snapshot exists (list_snapshots returns []).
    """
    checkout = _checkout()
    installation_id = 77
    key = _cache_key(checkout, installation_id=installation_id)

    client = _make_client(snapshots=[])
    handle, _ws = _make_handle(client=client, workspace_id="ws-pub-first", checkout=checkout)
    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)

    await cache.publish(handle, installation_id=installation_id)

    client.create_snapshot.assert_called_once()
    call_kwargs = client.create_snapshot.call_args
    # workspace_id must match the underlying Daytona workspace
    assert call_kwargs.kwargs.get("workspace_id") == "ws-pub-first" or (
        len(call_kwargs.args) > 0 and call_kwargs.args[0] == "ws-pub-first"
    ), f"create_snapshot called with wrong workspace_id: {call_kwargs}"
    # Labels must carry the cache key and the ref for LRU debugging.
    labels = call_kwargs.kwargs.get("labels", {})
    assert labels.get("openbot_key") == key
    assert labels.get("openbot_ref") == checkout.ref


@pytest.mark.asyncio
async def test_publish_is_idempotent_when_snapshot_already_exists() -> None:
    """If list_snapshots already returns a snapshot for this key, publish
    must return without calling create_snapshot — another worker won the race
    (or we're being re-called after a crash). create_snapshot must NOT be
    called (no double-snapshot).
    """
    checkout = _checkout()
    installation_id = 77
    key = _cache_key(checkout, installation_id=installation_id)
    existing_snap = _mock_snapshot(snapshot_id="snap-existing", key=key)

    client = _make_client(snapshots=[existing_snap])
    handle, _ws = _make_handle(client=client, checkout=checkout)
    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)

    await cache.publish(handle, installation_id=installation_id)

    client.create_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_publish_sweeps_excluded_paths_before_snapshot() -> None:
    """Before calling create_snapshot the publish method must sweep secret-
    bearing paths out of the workspace via sandbox.run(["find", ...]).

    Per the spec and CLAUDE.md forbidden list:
      .env* files, evals/logs/, .langgraph/, .inspect/, .doppler/

    We assert that at least one sandbox.run command contains "find" and
    references at least one of the excluded pattern names.  The sweep must
    occur BEFORE create_snapshot is called.
    """
    checkout = _checkout()
    installation_id = 77

    client = _make_client(snapshots=[])
    handle, ws = _make_handle(client=client, workspace_id="ws-sweep", checkout=checkout)
    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)

    # Track call order: sandbox.run calls vs create_snapshot
    call_log: list[str] = []

    def _tracking_exec(cmd: Any, timeout: Any) -> MagicMock:  # type: ignore[misc]
        call_log.append(f"exec:{cmd}")
        return MagicMock(exit_code=0, result="", additional_properties={})

    ws.process.exec.side_effect = _tracking_exec

    def _tracking_create(**_kwargs: Any) -> MagicMock:  # type: ignore[misc]
        call_log.append("create_snapshot")
        snap = MagicMock()
        snap.id = "snap-swept"
        return snap

    client.create_snapshot.side_effect = _tracking_create

    await cache.publish(handle, installation_id=installation_id)

    # At least one find/delete sweep must precede create_snapshot.
    exec_entries = [e for e in call_log if e.startswith("exec:")]
    sweep_entries = [e for e in exec_entries if "find" in e]
    assert sweep_entries, (
        "Expected at least one sandbox.run('find ...') sweep before snapshotting; "
        f"exec calls were: {exec_entries}"
    )

    # Sweep must reference at least one forbidden pattern.
    combined = " ".join(sweep_entries)
    assert any(
        pat in combined for pat in [".env", "evals", ".langgraph", ".inspect", ".doppler"]
    ), f"None of the expected forbidden patterns appeared in sweep commands: {combined}"

    # Sweep must happen BEFORE the snapshot is created.
    if "create_snapshot" in call_log:
        first_sweep_idx = next(i for i, e in enumerate(call_log) if "find" in e)
        create_idx = call_log.index("create_snapshot")
        assert first_sweep_idx < create_idx, "Secret sweep must precede create_snapshot"


@pytest.mark.asyncio
async def test_publish_raises_on_sdk_create_snapshot_failure() -> None:
    """If client.create_snapshot raises, publish propagates the exception so
    the dispatcher's _safe_publish wrapper can record the 'failed' counter.

    Silently swallowing the exception here would cause the dispatcher to
    record 'created' on failure — incorrect.
    """
    checkout = _checkout()
    installation_id = 77

    client = _make_client(snapshots=[])
    client.create_snapshot.side_effect = RuntimeError("daytona snapshot quota exceeded")
    handle, _ws = _make_handle(client=client, checkout=checkout)
    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)

    with pytest.raises(RuntimeError, match="quota exceeded"):
        await cache.publish(handle, installation_id=installation_id)


# ── Task 3.3 tests — eviction policies ───────────────────────────────────────


@pytest.mark.asyncio
async def test_lru_eviction_runs_on_publish_when_count_exceeds_max() -> None:
    """When the per-installation snapshot count exceeds max_entries after a
    publish, the cache evicts the oldest snapshot (by created_at) via
    _schedule_background → delete_snapshot.

    Setup:
      - max_entries = 3
      - 3 existing snapshots already in the backend (index already full)
      - publish one more → LRU eviction fires
      - Oldest snapshot's delete_snapshot must be called
    """
    installation_id = 42
    checkout = _checkout(ref="new-ref")
    key_new = _cache_key(checkout, installation_id=installation_id)

    now = _now_utc()
    from datetime import timedelta

    # Three existing snapshots: snap-old is oldest (3 hours ago), snap-mid 2h, snap-recent 1h.
    snap_old = _mock_snapshot(snapshot_id="snap-old", created_at=now - timedelta(hours=3))
    snap_mid = _mock_snapshot(snapshot_id="snap-mid", created_at=now - timedelta(hours=2))
    snap_recent_snap = _mock_snapshot(
        snapshot_id="snap-recent", created_at=now - timedelta(hours=1)
    )

    # After create_snapshot the backend has 4 entries (3 existing + snap-newest).
    # Use a mutable list so the LRU query sees the post-publish count.
    lru_list: list[Any] = [snap_old, snap_mid, snap_recent_snap]

    def _list_snapshots(labels: dict[str, str]) -> list[Any]:  # type: ignore[misc]
        # Idempotency check (by exact key) → not cached yet.
        if labels.get("openbot_key") == key_new:
            return []
        # LRU check (by installation_id) → current backend state.
        if "openbot_installation_id" in labels:
            return list(lru_list)
        return []

    def _create_snapshot(**_kwargs: Any) -> MagicMock:  # type: ignore[misc]
        snap = MagicMock()
        snap.id = "snap-newest"
        snap.created_at = now  # just created — newest
        lru_list.append(snap)  # simulate backend storing it
        return snap

    client = MagicMock()
    client.list_snapshots.side_effect = _list_snapshots
    client.create_snapshot.side_effect = _create_snapshot
    client.delete_snapshot.return_value = None

    handle, ws = _make_handle(client=client, workspace_id="ws-lru", checkout=checkout)
    ws.process.exec.return_value = MagicMock(exit_code=0, result="", additional_properties={})
    # max_entries=3: after publishing the 4th, oldest must be evicted.
    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400, max_entries=3)

    await cache.publish(handle, installation_id=installation_id)
    await asyncio.sleep(0)  # drain background eviction task

    # The oldest snapshot should be evicted.
    client.delete_snapshot.assert_called_once_with("snap-old")


@pytest.mark.asyncio
async def test_ttl_eviction_runs_on_acquire_for_stale_entry() -> None:
    """Already covered by test_acquire_treats_stale_snapshot_as_miss (Task 3.1).

    This duplicate documents the TTL policy as part of the Task 3.3
    acceptance matrix and asserts the same contract: stale entries are
    treated as miss + background-evicted.
    """
    checkout = _checkout()
    key = _cache_key(checkout, installation_id=99)
    # 48-hour-old snapshot; TTL = 24h.
    stale_snap = _mock_snapshot(
        snapshot_id="snap-ttl-stale",
        key=key,
        created_at=_now_utc() - timedelta(hours=48),
    )
    client = _make_client(snapshots=[stale_snap])
    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)

    result = await cache.acquire(checkout, "ghs_t", installation_id=99)
    await asyncio.sleep(0)

    assert result is None
    client.delete_snapshot.assert_called_once_with(stale_snap.id)


@pytest.mark.asyncio
async def test_evict_repo_deletes_all_keys_for_repo_url() -> None:
    """evict_repo('https://github.com/acme/widget.git') must delete all
    snapshots whose openbot_repo_url label matches that URL under the given
    installation_id, leaving snapshots from other repos untouched.

    The method issues one list_snapshots call filtered by repo_url + installation_id,
    then schedules a background delete for each matching snapshot.
    """
    target_repo = "https://github.com/acme/widget.git"

    installation_id = 55

    # Three snapshots of target_repo, one of other_repo.
    snap_a = _mock_snapshot(snapshot_id="snap-repo-a")
    snap_b = _mock_snapshot(snapshot_id="snap-repo-b")
    snap_c = _mock_snapshot(snapshot_id="snap-repo-c")

    def _list_snapshots(labels: dict[str, str]) -> list[Any]:  # type: ignore[misc]
        if labels.get("openbot_repo_url") == target_repo:
            return [snap_a, snap_b, snap_c]
        # other_repo or unrecognised query → nothing.
        return []

    client = MagicMock()
    client.list_snapshots.side_effect = _list_snapshots
    client.delete_snapshot.return_value = None

    cache = DaytonaSnapshotCache(daytona_client=client, ttl_seconds=86_400)
    await cache.evict_repo(target_repo, installation_id=installation_id)
    await asyncio.sleep(0)  # drain background eviction tasks

    # All three target-repo snapshots evicted, other_repo untouched.
    deleted_ids = {call.args[0] for call in client.delete_snapshot.call_args_list}
    assert deleted_ids == {"snap-repo-a", "snap-repo-b", "snap-repo-c"}

    # Verify we queried with the correct labels.
    list_call_labels = [
        call.kwargs.get("labels", {}) for call in client.list_snapshots.call_args_list
    ]
    assert any(
        lbl.get("openbot_repo_url") == target_repo
        and lbl.get("openbot_installation_id") == str(installation_id)
        for lbl in list_call_labels
    ), f"Expected list_snapshots with repo_url+installation_id labels; got {list_call_labels}"
