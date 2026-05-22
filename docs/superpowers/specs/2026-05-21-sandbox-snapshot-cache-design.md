# Sandbox Snapshot Cache — Sub-second sandbox acquire for grounded responders (design)

**Status:** design. Awaiting review before plan.
**Date:** 2026-05-21
**Branch (proposed):** `feat/sandbox-snapshot-cache`
**PRD anchors:** §3 (locked sandbox boundary; pluggable backends), §6 (per-task budgets), §8.2 (perf SLOs).
**Predecessor:** [`2026-05-21-unified-sandbox-entry-design.md`](./2026-05-21-unified-sandbox-entry-design.md) (Parts 1–3 landed). This spec exists because that slice's Part 4 placeholder was deferred to its own document.
**Blocks:** Parts 5–7 of the unified-sandbox-entry plan (triage repro, review grounded, chat code-grounding) at sustainable per-event cost.

---

## Goal

Make `_run_with_sandbox` return a `SandboxedHandle` ready to serve in **< 1 s P95** when the same `(repo_url, ref, strategy, sparse_paths)` tuple was acquired recently, by reusing a pre-warmed Daytona workspace snapshot instead of paying the full `factory() → clone()` cold-start (~10–15 s + $0.01–0.05 per event) every time.

Concretely:

1. Insert a **cache layer** between `ctx.sandbox_factory()` and `sandbox.clone(...)` in `dispatcher._run_with_sandbox`.
2. On **cache hit**: hydrate a sandbox from the existing snapshot, run a single fast `git fetch + reset` to advance to the requested ref, return the handle.
3. On **cache miss**: run the existing cold-clone path; after the handler finishes, asynchronously snapshot the workspace and write the cache entry.
4. **Failure is invisible to the caller**: any cache error falls through to the cold path — the cache is an optimization, never a correctness boundary.
5. **Bounded resource cost**: LRU eviction by snapshot count + time-based eviction by age + per-repo invalidation when the default branch advances far enough that the snapshot is no longer useful.

This is the prerequisite for Parts 5–7 of the unified-sandbox-entry slice to ship without a per-event cost regression: chat is the highest-volume workflow and the one whose unit economics most sensitive to cold-start.

---

## Locked decisions

| Topic | Decision | Rationale |
|---|---|---|
| Layer | **New `SandboxCachePort` in `openbot/application/ports/sandbox_cache.py`**; dispatcher consults it before `ctx.sandbox_factory()`. | Hexagonal boundary preserved. Daytona-specific snapshot mechanics stay in `infrastructure/`. Tests target the port. |
| Acquire shape | **`acquire(checkout: CheckoutSpec, token: str) -> SandboxedHandle \| None`**. Returns `None` on miss; dispatcher then runs the existing cold path. | `None`-as-miss makes the dispatcher's branching obvious; no exceptions for "not cached" (exceptions are reserved for backend errors). |
| Publish shape | **`publish(handle: SandboxedHandle) -> None`** invoked after the handler finishes (success or failure). | Decouples "did the handler succeed?" from "is this workspace useful to cache?". Even failed handlers leave a warm tree worth caching for the next event. |
| Cache key | **`(repo_url, ref, strategy, tuple(sparse_paths))`** → SHA-256 → 16-hex-char prefix. | `strategy` is non-negotiable: `BLOBLESS` vs `SHALLOW` for the same ref produce different working-tree shapes; collapsing them corrupts read paths. `sparse_paths` similarly varies the tree. `diff_base` is NOT part of the key — it's metadata for the handler, not a tree-shape input. |
| Backend | **Daytona snapshot API in production; in-process "warm pool" in `FakeSandboxCacheAdapter` for tests.** Modal / Docker backends get NoOp implementations. | Daytona is the only production backend in v0.1 per PRD §3. Modal/Docker live in `evals/` and don't share the production port. |
| Eviction policy | **LRU by snapshot count (default 50) + TTL 24h + per-repo `git fetch` staleness gate (described below).** | Each policy guards a different failure mode (memory growth / drift / wrong-tree). Combining them avoids the "we have 200 24-hour-old snapshots of the same repo at SHAs nobody asks for" failure. |
| Ref-advance handling | **Cache key is on `ref`, not on `repo_url`.** A new SHA → new cache entry. Within an entry, we never `git pull` on hit; we always `git fetch + reset --hard ref` to enforce determinism. | Caching `(repo, branch)` is a footgun — the working tree races with upstream pushes. Caching `(repo, sha)` is content-addressed and immutable. |
| Snapshot creation | **Asynchronous, fire-and-forget after handler returns.** | Cold path takes ~10 s; adding snapshot serialization to the critical path would *increase* P99 latency on miss. |
| Cache miss observability | **Counter + histogram split on `{result=hit|miss|stale|backend_error}`.** | Operators need to distinguish "we never cached this" from "we had it but it expired" from "Daytona timed out". |
| Security boundary | **Tokens never touch the cache.** Snapshots contain repository content (already present in the working tree); tokens are injected per-acquire via the existing `_inject_token` flow. | Token lifetime ≤ 1h, snapshot lifetime ≤ 24h: storing the token in the snapshot would extend its blast radius. |
| Cross-tenant safety | **One cache namespace per `installation_id`**; cache key prefix includes the installation. | v0.1 has one bot install per dispatch, but the constraint costs nothing now and prevents a future multi-tenant leak. |
| Rollout | **Gated behind `OPENBOT_SANDBOX_CACHE_ENABLED` (default `false`).** Enabled per-workflow via `OPENBOT_SANDBOX_CACHE_FEATURES=chat,triage` for incremental rollout. | The cache is an optimization; the cold path must remain the proven correctness baseline. Per-feature enablement lets us roll chat → triage → review → fix as confidence grows. |

---

## Architecture

### Layer map (hexagonal contract preserved)

```
application/
  ports/
    sandbox_cache.py             # NEW: SandboxCachePort protocol
  dispatcher.py                  # GROWS: _run_with_sandbox consults ctx.sandbox_cache before factory()
  middleware/
    preflight.py                 # GROWS: PreflightContext gains optional sandbox_cache: SandboxCachePort | None

infrastructure/
  sandboxes/
    cache_daytona.py             # NEW: DaytonaSnapshotCache (uses Daytona snapshot/template API)
    cache_fake.py                # NEW: InMemorySandboxCache (warm-pool of FakeSandboxAdapter instances; tests only)
    cache_noop.py                # NEW: NoOpSandboxCache (returns None always; default for non-Daytona backends)
  observability/
    metrics.py                   # GROWS: sandbox_cache_total{feature,result}, sandbox_cache_acquire_seconds histogram

eps/
  cli/                           # No change
  github/                        # No change

tests/
  application/
    test_dispatcher_cache.py     # NEW: dispatcher integration — hit / miss / stale / backend_error
  infrastructure/
    sandboxes/
      test_cache_daytona.py      # NEW: Daytona adapter unit tests w/ SDK mocked
      test_cache_fake.py         # NEW: in-memory adapter LRU / TTL behaviour
```

### Per-event flow (cache layered in)

```
… preflight … classifier … policy gate …  (unchanged)
    ↓
[NEW] cache_hit: SandboxedHandle | None = (
          await ctx.sandbox_cache.acquire(checkout, token)
          if ctx.sandbox_cache is not None
          else None
      )
    ↓
    ├── hit  → sandboxed_ctx = dataclasses.replace(ctx, sandbox_handle=cache_hit, ...)
    │         → await dispatch.handler(sandboxed_ctx)
    │         → ctx.sandbox_cache.publish(cache_hit)  # refresh LRU position
    │         → return
    │
    └── miss → [EXISTING] async with ctx.sandbox_factory() as sandbox:
                  await sandbox.clone(repo_url=..., ref=..., token=..., strategy=...)
                  handle = SandboxedHandle(sandbox, checkout, token)
                  sandboxed_ctx = dataclasses.replace(ctx, sandbox_handle=handle, ...)
                  try:
                      await dispatch.handler(sandboxed_ctx)
                  finally:
                      if ctx.sandbox_cache is not None:
                          asyncio.create_task(
                              ctx.sandbox_cache.publish(handle)
                          )                          # fire-and-forget; never blocks
```

Note: the `publish` call on cache **hit** updates LRU recency but does not re-snapshot (the snapshot is already canonical). On **miss** the `publish` triggers asynchronous snapshot creation.

---

## Type contract

```python
# openbot/application/ports/sandbox_cache.py
from typing import Protocol, runtime_checkable
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.domain.checkout import CheckoutSpec


@runtime_checkable
class SandboxCachePort(Protocol):
    """Optional cache layer between dispatcher and SandboxPort factory.

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
        On hit, returns a handle whose sandbox.workspace is checked out
        at ``checkout.ref`` — implementations MUST run a fast
        ``git fetch && git reset --hard {ref}`` between hydrate and
        return to guarantee tree state matches ``ref``.
        """
        ...

    async def publish(
        self,
        handle: SandboxedHandle,
        *,
        installation_id: int,
    ) -> None:
        """Record this handle as cacheable. May snapshot synchronously
        or schedule async work. Idempotent: publishing the same
        (key, snapshot_content) twice MUST NOT create two entries.
        """
        ...

    async def evict_repo(self, repo_url: str, *, installation_id: int) -> None:
        """Invalidate all snapshots for ``repo_url`` under this installation.
        Called by webhook ingest when the default branch advances by more
        than ``OPENBOT_SANDBOX_CACHE_INVALIDATION_COMMITS`` (default: 100).
        """
        ...
```

The `SandboxedHandle` shape is unchanged — the cache returns the same `(sandbox, checkout, token)` triple the cold path returns. Handlers see no difference.

### Cache key derivation

```python
def _cache_key(
    checkout: CheckoutSpec,
    *,
    installation_id: int,
) -> str:
    """Deterministic 24-char hex prefix identifying a snapshot.

    Components, in order:
      installation_id : tenant scope (prevents cross-installation reuse)
      repo_url        : normalized to lowercase, trailing slash stripped
      ref             : exact SHA (never a branch name)
      strategy.value  : BLOBLESS / SHALLOW / SHALLOW_HISTORY / FULL
      sparse_paths    : tuple → tab-joined string (stable order required)
    """
    components = "\t".join([
        str(installation_id),
        checkout.repo_url.rstrip("/").lower(),
        checkout.ref,
        checkout.strategy.value,
        "\t".join(sorted(checkout.sparse_paths)),
    ])
    return hashlib.sha256(components.encode()).hexdigest()[:24]
```

`sparse_paths` is sorted so `("a", "b")` and `("b", "a")` produce the same key (sparse-checkout is set-semantics, not list-semantics). Everything else preserves order because order matters (SHA is order-dependent; URL components are positional).

---

## Resolution algorithm (acquire path)

```
1. key = _cache_key(checkout, installation_id=...)
2. snapshot_meta = await backend.lookup(key)
3. if snapshot_meta is None:
       counter("sandbox_cache_total", result="miss").inc()
       return None
4. if (now - snapshot_meta.created_at) > TTL:
       counter("sandbox_cache_total", result="stale").inc()
       await backend.evict(key)        # fire-and-forget acceptable
       return None
5. try:
       sandbox = await backend.hydrate(snapshot_meta)
   except BackendError as e:
       counter("sandbox_cache_total", result="backend_error").inc()
       logger.warning("cache hydrate failed: %s", e)
       return None
6. await _refresh_to_ref(sandbox, checkout, token)
7. counter("sandbox_cache_total", result="hit").inc()
   histogram("sandbox_cache_acquire_seconds").observe(elapsed)
   return SandboxedHandle(sandbox, checkout, token)
```

Step 6 is the critical correctness gate:

```python
async def _refresh_to_ref(sandbox, checkout, token) -> None:
    """Ensure the hydrated sandbox is at exactly ``checkout.ref``.

    Even though the cache key includes ``ref``, the snapshot may have
    been created at the same ref but the working tree may have drifted
    if any earlier handler touched files (write_file, commit). Re-asserting
    the ref defends against that.
    """
    authed_url = _inject_token(checkout.repo_url, token)
    await sandbox.run(command=["git", "remote", "set-url", "origin", authed_url])
    await sandbox.run(command=["git", "fetch", "--depth=1", "origin", checkout.ref])
    result = await sandbox.run(command=["git", "reset", "--hard", checkout.ref])
    if result.exit_code != 0:
        raise CacheCorruptedError(f"could not reset to {checkout.ref}: {result.stderr}")
```

A `CacheCorruptedError` is caught at the dispatcher boundary and treated as a miss — cache invalidates the entry and the cold path runs. This is the **only** correctness-affecting failure mode and it's explicitly handled.

---

## Publish (snapshot creation)

Asynchronous. Triggered by the dispatcher on cache miss after the handler returns:

```python
async def publish(self, handle: SandboxedHandle, *, installation_id: int) -> None:
    key = _cache_key(handle.checkout, installation_id=installation_id)
    if await self._backend.lookup(key) is not None:
        return                        # already cached; another worker won the race
    # Daytona's snapshot API: create a workspace template from the live workspace.
    snapshot_id = await self._backend.create_snapshot(
        workspace_id=handle.sandbox.workspace_id,
        labels={"openbot_key": key, "openbot_ref": handle.checkout.ref},
    )
    await self._backend.write_index(key, snapshot_id, created_at=now())
```

**Snapshot content**:

- `.git/` directory (full, NOT shallow — the snapshot is the warm pool, future refreshes do their own shallow fetch).
- Working tree at `ref`.
- No node_modules / .venv / .pyc — those are workflow-specific install artifacts; the responder re-runs `pip install -e .` etc. on hit if it needs to.

**Snapshot exclusions** (the negative space is as important as the positive):

- No tokens (already enforced — tokens are injected per-acquire).
- No `.env*` files (existing repo `.gitignore` discipline; reinforced by snapshot allowlist).
- No `evals/logs/`, `.langgraph/`, `.inspect/`, `.doppler/` (per `CLAUDE.md` forbidden list — defence-in-depth at the cache layer).

---

## Eviction

Three policies, all running independently. The cache is correct under any combination; over-aggressive eviction degrades hit rate, under-aggressive eviction wastes resources but doesn't corrupt anything.

### 1. LRU by count (per installation)

`OPENBOT_SANDBOX_CACHE_MAX_ENTRIES` default 50. On publish, if `count > max`, evict the least-recently-acquired entry. Implementation: backend maintains a doubly-linked list of `(key, last_access)` tuples.

### 2. TTL (per entry)

`OPENBOT_SANDBOX_CACHE_TTL_SECONDS` default 86_400 (24h). Snapshots older than TTL are treated as miss + evicted async. Rationale: repo state drifts; old snapshots are likely to be on stale refs nobody asks for.

### 3. Repo-level invalidation (per default-branch advance)

When the GitHub ingest layer sees a `push` event on the default branch and the new head is more than `OPENBOT_SANDBOX_CACHE_INVALIDATION_COMMITS` (default 100) commits ahead of the most-recently-cached SHA for that repo, all snapshots for that repo are evicted. Implements `evict_repo(repo_url)` on the port.

Rationale: even though `ref`-based caching is content-addressed, *useful* refs evolve. A repo that's had 200 commits land since the snapshot was created has likely moved past the snapshot's working set, and the storage is better used by other repos.

---

## Observability

### Counters

| Metric | Labels | Increments on |
|---|---|---|
| `openbot_sandbox_cache_total` | `feature`, `result` | `result ∈ {hit, miss, stale, backend_error}` per acquire |
| `openbot_sandbox_cache_publish_total` | `feature`, `result` | `result ∈ {created, exists, failed}` per publish |
| `openbot_sandbox_cache_eviction_total` | `policy` | `policy ∈ {lru, ttl, repo_advance}` |

### Histograms

| Metric | Buckets | Records |
|---|---|---|
| `openbot_sandbox_cache_acquire_seconds` | exponential 0.1 → 60 | Time from `acquire()` start to handle returned. Includes refresh-to-ref. |
| `openbot_sandbox_cache_publish_seconds` | exponential 1 → 300 | Time from `publish()` start to snapshot persisted. |

Acceptance: `acquire_seconds{result="hit"}` P95 < 1 s. `acquire_seconds{result="miss"}` ≈ current cold-clone P95 (no regression; the cache check is a single index lookup).

### Logs

Structured fields on every cache event: `cache_key` (truncated 8 chars; not full key — log volume concern), `result`, `feature`, `repo`, `ref` (short SHA), `installation_id`.

Sentry breadcrumb mirror via existing `_LabelledSentryCounter` wrapper (the same pattern used for `dispatch_sandbox_total` in Part 2.3).

---

## Failure-mode matrix

| Failure | Behavior | User-visible? |
|---|---|---|
| `ctx.sandbox_cache is None` (cache not wired) | Skip acquire; cold path runs | No — identical to today |
| `backend.lookup` raises | Log + counter, treat as miss | No |
| `backend.hydrate` raises | Log + counter, treat as miss, async-evict the index entry | No |
| `_refresh_to_ref` fails on `git fetch` | Treat as miss; cold path runs; cache entry evicted | No |
| `_refresh_to_ref` fails on `git reset --hard` (corrupted tree) | Raise `CacheCorruptedError`, caught at dispatcher → treat as miss; entry evicted; Sentry alert (level=warning) | No — but ops gets paged if rate > 1/min |
| `publish` snapshot creation fails | Log + counter; handler already returned, user already replied | No |
| Daytona quota exhausted (snapshot count limit) | LRU eviction is supposed to prevent this; if it happens, treat as backend_error | No — but ops gets paged (level=error) |

The single non-graceful failure surface is `CacheCorruptedError` because it signals that our content-addressing assumption broke. It's caught, treated as miss, but generates an alert because it should never happen.

---

## Test strategy

### Unit

- `_cache_key` is deterministic, order-stable for `sparse_paths`, varies on every component.
- `_refresh_to_ref` runs the right git commands and raises on non-zero exit.
- LRU eviction picks the right entries.
- TTL eviction respects the configured threshold.

### Integration (dispatcher)

Parametrized matrix in `tests/application/test_dispatcher_cache.py`:

| Scenario | `cache.acquire` returns | Expected handler invocation | Expected counters |
|---|---|---|---|
| miss → handler success | `None` | Cold path; handle with workspace-from-factory | `cache_total{result=miss}`, `cache_publish_total{result=created}` |
| miss → handler raises | `None` | Cold path; handle with workspace-from-factory; publish still runs | `cache_total{result=miss}`, `cache_publish_total{result=created}` |
| hit | `SandboxedHandle(...)` | Handler invoked with hit handle; factory NOT called | `cache_total{result=hit}` |
| stale (`acquire` returns None after TTL eviction) | `None` | Cold path | `cache_total{result=stale}` |
| backend error during acquire | raises `BackendError` | Cold path; log line emitted | `cache_total{result=backend_error}` |
| `ctx.sandbox_cache is None` | (not called) | Cold path; factory called as today | (no cache metrics emitted) |

### E2E

- New demo `tests/e2e/test_spec_demos.py::test_demo_12_chat_cache_hit_under_one_second`: ask the same question on the same PR twice in a row; assert second response time < 1 s (gated on `OPENBOT_SANDBOX_CACHE_ENABLED=true`; default-skipped on CI without the env var).

### Eval

No eval surface — this is a perf/cost optimization, not an LLM behavior change.

---

## Migration plan

| Phase | Trigger | Scope | Rollback |
|---|---|---|---|
| 1 | Spec merges + Plan lands | Build infra; default `OPENBOT_SANDBOX_CACHE_ENABLED=false` everywhere. Cold path is the only production path. | n/a — feature is dead code |
| 2 | Telemetry confirms cold-start P95 + per-event cost | Enable for `chat` only via `OPENBOT_SANDBOX_CACHE_FEATURES=chat`. Monitor `cache_total{result}` ratio + `acquire_seconds{result=hit}` histogram for 1 week. | Flip env back to `false` |
| 3 | If chat shows ≥ 50% hit rate and < 1 s P95 on hits | Add `triage` to the env list. Same monitoring window. | Per-feature env removal |
| 4 | Both stable | Add `review` + `fix`. | Per-feature env removal |
| 5 | Confident | Default `OPENBOT_SANDBOX_CACHE_ENABLED=true` in deployment config; remove env-gate at code level in a future cleanup. | Code revert |

Rollout matches the risk gradient: chat is read-only + highest volume + lowest stakes; fix is the most-write-heavy + highest stakes. We earn confidence at each phase before broadening.

---

## Risks & open questions

### Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Daytona snapshot API rate limits or quotas we haven't characterized | High | Phase 2 of rollout includes a 1-week observation window. If we hit limits, fall back to in-process warm pool (slower hit, no cross-worker reuse). |
| Snapshot includes secrets a contributor accidentally committed | High | Snapshot exclusion allowlist + `.gitignore` semantics + secret-scanning gate at publish (truffleHog rule subset). |
| Worker race: two workers acquire the same miss simultaneously, both run cold path, both publish | Medium | `publish` is idempotent (lookup-then-write); the second writer's snapshot is discarded. Wasted compute = 1 redundant cold clone in the racing window. Acceptable. |
| Cache becomes a security boundary by accident (e.g. someone caches the installation token) | High | Code review checklist: no token-typed value crosses `publish()`. Test asserts `_cache_key` rejects tokens. Periodic snapshot content audit (manual, quarterly). |
| Cache hit rate dramatically below expectations (e.g. < 10%) | Medium | Phase 2 monitoring catches it; we'd disable and reassess. No data loss; no rollback complexity. |
| `CacheCorruptedError` rate is non-zero | Medium | Single failure mode is well-defined; alert-on-rate (> 1/min) makes it actionable. Worst case: we disable cache, no correctness impact. |

### Open questions (resolve before Plan)

1. **Daytona snapshot pricing model**: do snapshots cost storage continuously, or only on hydrate? Affects LRU max-entries default. Action: contact Daytona / read pricing docs before plan.
2. **Cross-worker hit accounting**: should `acquire_seconds{result=hit}` be measured from the requesting worker's perspective (network RTT included) or the cache's perspective (server-side hydrate only)? Default: worker-side, matches user-visible latency. Confirm during plan.
3. **Should `repo_advance` invalidation be implemented in v1, or pushed to a follow-up?** TTL alone covers most drift; `repo_advance` is belt-and-suspenders. Default: include in v1 to avoid follow-up debt. Confirm during plan.
4. **Sentry alert thresholds for `CacheCorruptedError` and `backend_error`**: 1/min seems right; needs ops sign-off.

---

## Acceptance for the whole slice

- [ ] `SandboxCachePort` defined; `DaytonaSnapshotCache`, `InMemorySandboxCache`, `NoOpSandboxCache` implemented.
- [ ] Dispatcher `_run_with_sandbox` consults cache; cold path unchanged on `cache is None`.
- [ ] `make check` green at every commit; no `--no-verify`.
- [ ] Hexagonal contract held (lint-imports clean).
- [ ] Unit + integration test matrix green.
- [ ] E2E demo `test_demo_12_chat_cache_hit_under_one_second` passes locally with `OPENBOT_SANDBOX_CACHE_ENABLED=true`.
- [ ] Phase-1 enablement (cache=false everywhere) deployed to prod. Cold path remains the proven baseline.
- [ ] Snapshot security audit: manual review of one production snapshot to confirm no tokens / .env / secret-class files leaked.
- [ ] This spec + the resulting plan archived to `docs/_archive/superpowers/` once Phase 4 rollout is complete.

---

## Why this design over the alternatives

### Alternative A: Cache inside `DaytonaSandboxAdapter.clone`

*Looks simpler — no new port — but locks the cache to Daytona.* Modal / Docker backends would have no cache surface; tests can't substitute an in-memory cache without faking the entire `SandboxPort`. Hexagonal cost: the dispatcher would lose the ability to reason about cache hit/miss because the call shape stays `factory().clone()` regardless. Rejected.

### Alternative B: Process-wide LRU cache of fully-constructed `SandboxedHandle`s

*Lowest latency on hit (microseconds) but maximum risk.* A `SandboxedHandle` holds a live remote workspace; pooling them across events means concurrent handlers could touch the same workspace. Solving the concurrency would require per-handle locking, which negates the latency win. Rejected.

### Alternative C: Build-time pre-warming (snapshot the top-N repos at deploy time)

*Predictable cost but high maintenance.* The "top-N repos" set drifts; manual curation is the wrong feedback loop for a multi-installation bot. Rejected as a primary design, but kept as a future optimization on top of the run-time cache.

### Alternative D: Skip caching; use a CDN-backed shallow-clone mirror

*Outside our control surface.* Would require operating a git mirror, which is a separate product. The latency win is real but the operational complexity is disproportionate to the cost savings at v0.1 scale. Rejected.

The chosen design (port + per-backend adapter + dispatcher-level integration) is the smallest design that preserves the hexagonal contract, allows independent backend evolution, and rolls out incrementally per workflow.
