"""InMemorySandboxCache — LRU + TTL + concurrent-publish invariants.

The in-memory cache is the tests-and-dev sibling of ``DaytonaSnapshotCache``
(Part 3). It holds live sandboxes in a process-local index so the same
``(installation_id, checkout)`` returns the same workspace within the
TTL, refreshed to the requested ref. Coverage targets every behavior
the dispatcher relies on:

  - **Miss/hit roundtrip** — empty cache misses; publish-then-acquire hits.
  - **Refresh contract** — every acquire ends with the workspace at
    exactly ``checkout.ref`` via ``git fetch && git reset --hard <ref>``.
    A failed refresh raises ``CacheCorruptedError`` internally; the
    public ``acquire`` translates that into miss + eviction.
  - **Idempotent publish** — repeated publishes of the same key produce
    one entry, not many.
  - **LRU eviction** — when ``max_entries`` is exceeded, the
    least-recently-used entry is dropped. Recency is bumped on hit.
  - **TTL eviction** — entries older than ``ttl_seconds`` are treated
    as miss and pruned on access.
  - **Per-repo invalidation** — ``evict_repo`` drops every key for one
    ``repo_url`` under one installation, leaving other repos alone.
  - **Concurrent publish safety** — ``asyncio.gather`` 10 publishes of
    the same key → exactly one entry survives.

Per Task 1.4 of ``docs/superpowers/plans/2026-05-21-sandbox-snapshot-cache-plan.md``
and § "Resolution algorithm" of the predecessor spec.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from openbot.application.ports.sandbox import ExecResult
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.domain.checkout import CheckoutSpec, CloneStrategy
from openbot.infrastructure.sandboxes.cache_fake import InMemorySandboxCache

_REF_A = "a" * 40
_REF_B = "b" * 40


class _ScriptedSandbox:
    """SandboxPort stub that records every ``run`` call.

    The cache's ``_refresh_to_ref`` issues a sequence of ``run`` calls
    (set-url, fetch, reset). Tests assert on the recorded command list
    instead of standing up a real git repo. ``script`` is a list of
    ``ExecResult`` values returned in order; the default is "every
    command succeeds with exit 0".
    """

    def __init__(self, workspace: str = "/tmp/scripted", *, script: list[ExecResult] | None = None):
        self.workspace = workspace
        self.calls: list[list[str]] = []
        self._script = script or []
        self._closed = False

    async def clone(
        self,
        *,
        repo_url: str,
        ref: str,
        token: str,
        strategy: CloneStrategy = CloneStrategy.SHALLOW,
    ) -> None:
        return None

    async def read_file(self, path: str) -> str:
        return ""

    async def write_file(self, path: str, content: str) -> None:
        return None

    async def list_files(self, *, path: str = ".", max: int = 200) -> list[str]:
        return []

    async def run(
        self,
        *,
        command: list[str],
        timeout_seconds: int = 60,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        self.calls.append(list(command))
        if self._script:
            return self._script.pop(0)
        return ExecResult(stdout="", stderr="", exit_code=0, timed_out=False)

    async def git_diff(self) -> str:
        return ""

    async def commit_and_push(self, *, branch_ref: str, message: str, token: str) -> None:
        return None

    async def close(self) -> None:
        self._closed = True


def _spec(
    ref: str = _REF_A, repo_url: str = "https://github.com/foo/bar.git", **overrides: Any
) -> CheckoutSpec:
    defaults: dict[str, Any] = {
        "repo_url": repo_url,
        "ref": ref,
        "strategy": CloneStrategy.SHALLOW,
        "diff_base": None,
        "sparse_paths": (),
    }
    defaults.update(overrides)
    return CheckoutSpec(**defaults)


def _handle(sandbox: _ScriptedSandbox, spec: CheckoutSpec) -> SandboxedHandle:
    return SandboxedHandle(sandbox=sandbox, checkout=spec, token="ghs_xxx")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_first_acquire_is_miss() -> None:
    cache = InMemorySandboxCache(max_entries=10, ttl_seconds=60)
    result = await cache.acquire(_spec(), token="ghs_xxx", installation_id=42)
    assert result is None


@pytest.mark.asyncio
async def test_publish_then_acquire_hits() -> None:
    cache = InMemorySandboxCache(max_entries=10, ttl_seconds=60)
    sandbox = _ScriptedSandbox()
    spec = _spec()
    await cache.publish(_handle(sandbox, spec), installation_id=42)

    result = await cache.acquire(spec, token="ghs_yyy", installation_id=42)
    assert result is not None
    assert result.sandbox is sandbox
    assert result.checkout == spec
    assert result.token == "ghs_yyy"  # acquire-time token, not publish-time


@pytest.mark.asyncio
async def test_acquire_runs_refresh_to_ref() -> None:
    # Pin the refresh sequence the spec mandates: set-url with token,
    # fetch, reset --hard <ref>. If this fails, hot snapshots could
    # serve stale working trees to handlers — correctness bug.
    cache = InMemorySandboxCache(max_entries=10, ttl_seconds=60)
    sandbox = _ScriptedSandbox()
    spec = _spec(ref=_REF_A)
    await cache.publish(_handle(sandbox, spec), installation_id=42)
    sandbox.calls.clear()  # discard any publish-time bookkeeping

    await cache.acquire(spec, token="ghs_secret", installation_id=42)

    # 1) origin URL gets the freshly-supplied token interpolated.
    # 2) fetch the requested ref.
    # 3) reset --hard to that ref so the working tree matches exactly.
    assert any(
        cmd[:3] == ["git", "remote", "set-url"] and "x-access-token:ghs_secret@" in cmd[-1]
        for cmd in sandbox.calls
    ), f"expected origin URL injection; got {sandbox.calls!r}"
    assert any(cmd[:2] == ["git", "fetch"] for cmd in sandbox.calls)
    assert any(cmd[:3] == ["git", "reset", "--hard"] and cmd[-1] == _REF_A for cmd in sandbox.calls)


@pytest.mark.asyncio
async def test_acquire_evicts_and_misses_on_refresh_failure() -> None:
    # If `git reset --hard` returns non-zero, the snapshot is corrupted.
    # The adapter must evict the entry and report a miss; the dispatcher
    # then falls through to the cold path. A subsequent acquire must
    # also miss (entry really gone, not just hidden).
    cache = InMemorySandboxCache(max_entries=10, ttl_seconds=60)
    sandbox = _ScriptedSandbox(
        script=[
            ExecResult(stdout="", stderr="", exit_code=0, timed_out=False),  # set-url
            ExecResult(stdout="", stderr="", exit_code=0, timed_out=False),  # fetch
            ExecResult(stdout="", stderr="bad ref", exit_code=128, timed_out=False),  # reset fails
        ]
    )
    spec = _spec()
    await cache.publish(_handle(sandbox, spec), installation_id=42)

    result = await cache.acquire(spec, token="ghs_xxx", installation_id=42)
    assert result is None

    # Entry truly gone — second acquire is a clean miss with no refresh attempt.
    sandbox.calls.clear()
    second = await cache.acquire(spec, token="ghs_xxx", installation_id=42)
    assert second is None
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_publish_is_idempotent() -> None:
    # Two publishes of the same (install, checkout) must keep ONE entry,
    # not two. The dispatcher does not guard against double-publish; the
    # adapter must.
    cache = InMemorySandboxCache(max_entries=10, ttl_seconds=60)
    spec = _spec()
    sandbox1 = _ScriptedSandbox(workspace="/tmp/one")
    sandbox2 = _ScriptedSandbox(workspace="/tmp/two")

    await cache.publish(_handle(sandbox1, spec), installation_id=42)
    await cache.publish(_handle(sandbox2, spec), installation_id=42)

    assert cache.size() == 1


@pytest.mark.asyncio
async def test_lru_evicts_oldest_when_max_exceeded() -> None:
    cache = InMemorySandboxCache(max_entries=2, ttl_seconds=60)
    spec_a = _spec(ref="a" * 40)
    spec_b = _spec(ref="b" * 40)
    spec_c = _spec(ref="c" * 40)

    await cache.publish(_handle(_ScriptedSandbox("/a"), spec_a), installation_id=42)
    await cache.publish(_handle(_ScriptedSandbox("/b"), spec_b), installation_id=42)
    await cache.publish(_handle(_ScriptedSandbox("/c"), spec_c), installation_id=42)

    # A was oldest, must be evicted. B and C remain.
    assert await cache.acquire(spec_a, token="x", installation_id=42) is None
    assert await cache.acquire(spec_b, token="x", installation_id=42) is not None
    assert await cache.acquire(spec_c, token="x", installation_id=42) is not None


@pytest.mark.asyncio
async def test_ttl_evicts_stale_entries() -> None:
    # TTL = 0 means every entry is stale on acquire. Cleaner than
    # sleeping and avoids flake on slow CI.
    cache = InMemorySandboxCache(max_entries=10, ttl_seconds=0)
    spec = _spec()
    await cache.publish(_handle(_ScriptedSandbox(), spec), installation_id=42)

    result = await cache.acquire(spec, token="x", installation_id=42)
    assert result is None
    assert cache.size() == 0  # pruned on access


@pytest.mark.asyncio
async def test_evict_repo_clears_keys_for_one_repo_only() -> None:
    cache = InMemorySandboxCache(max_entries=10, ttl_seconds=60)
    spec_repo1_a = _spec(repo_url="https://github.com/foo/one.git", ref="a" * 40)
    spec_repo1_b = _spec(repo_url="https://github.com/foo/one.git", ref="b" * 40)
    spec_repo2 = _spec(repo_url="https://github.com/foo/two.git", ref="c" * 40)

    await cache.publish(_handle(_ScriptedSandbox("/1a"), spec_repo1_a), installation_id=42)
    await cache.publish(_handle(_ScriptedSandbox("/1b"), spec_repo1_b), installation_id=42)
    await cache.publish(_handle(_ScriptedSandbox("/2"), spec_repo2), installation_id=42)

    await cache.evict_repo("https://github.com/foo/one.git", installation_id=42)

    assert await cache.acquire(spec_repo1_a, token="x", installation_id=42) is None
    assert await cache.acquire(spec_repo1_b, token="x", installation_id=42) is None
    assert await cache.acquire(spec_repo2, token="x", installation_id=42) is not None


@pytest.mark.asyncio
async def test_concurrent_publish_is_safe() -> None:
    # 10 simultaneous publishes of the same key — must end with exactly
    # one entry. Naive dict mutation would race on the "exists?" check.
    cache = InMemorySandboxCache(max_entries=10, ttl_seconds=60)
    spec = _spec()
    sandboxes = [_ScriptedSandbox(f"/s{i}") for i in range(10)]

    await asyncio.gather(
        *(cache.publish(_handle(sb, spec), installation_id=42) for sb in sandboxes)
    )

    assert cache.size() == 1
