# Sandbox Snapshot Cache — Implementation plan (parts 1–4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Source spec:** [`docs/superpowers/specs/2026-05-21-sandbox-snapshot-cache-design.md`](../specs/2026-05-21-sandbox-snapshot-cache-design.md)
**Branch (proposed):** `feat/sandbox-snapshot-cache`
**Goal:** Insert a `SandboxCachePort` between `dispatcher._run_with_sandbox` and `ctx.sandbox_factory`, so grounded responders introduced by Parts 5–7 of the unified-sandbox-entry slice can serve in < 1 s P95 on repeat events instead of paying ~10–15 s cold-start each time.

**Tech stack constraints (do not violate):**
- Python 3.12, pytest, `make check` green at every commit.
- No `--no-verify` on pre-commit hooks.
- Cache is an optimization; the cold path is the proven correctness baseline and must remain reachable via env gate.
- Hexagonal contract: Daytona-specific snapshot code lives in `infrastructure/sandboxes/cache_daytona.py`; application layer only knows the `SandboxCachePort` protocol. `lint-imports` must stay green.
- Tokens never enter snapshots or cache index — only repository content is cached.
- Observability = LangSmith + Prometheus + Sentry mirror via existing `_LabelledSentryCounter` wrapper.

---

## Status checkpoint (template — fill on land)

| Part | Status | Commits |
|---|---|---|
| 1 — Port + key + in-memory cache | ✅ complete (1.1 `28c20b3`, 1.2 `78c14e2`, 1.3 `4201223`, 1.4 `19fab6a`) | 28c20b3…19fab6a |
| 2 — Dispatcher wiring + observability | ✅ complete (2.1 `62b9686`, 2.2 `77b3e1d`, 2.3 `f9845d4`) | 62b9686…f9845d4 |
| 3 — Daytona snapshot adapter | ⏳ pending | — |
| 4 — Rollout, guardrails, E2E | ⏳ pending | — |

---

## Per-PR slicing

| Part | Theme | Touches dispatcher? | Behavior change? |
|---|---|---|---|
| 1 | Port + key + in-memory cache | No | None — additive only |
| 2 | Dispatcher wiring + observability | Yes | None when `sandbox_cache is None` (default); cache path runs only when wired |
| 3 | Daytona snapshot adapter | No | Cost-only — same handler, faster acquire on hit |
| 4 | Rollout, guardrails, E2E | DI only | Behind `OPENBOT_SANDBOX_CACHE_ENABLED` env (default false) |

Parts 1–3 land independently as small PRs. Part 4 wires them together behind a feature flag and adds the E2E demo + security gates. The cold path remains the production default until ops opts in via env.

---

## Type & symbol contract (locked across the plan)

| Symbol | Defined in | Used by |
|---|---|---|
| `SandboxCachePort` (Protocol, runtime_checkable) | `openbot/application/ports/sandbox_cache.py` (Part 1) | Dispatcher (Part 2), every adapter (Parts 1, 3) |
| `_cache_key(checkout, *, installation_id) -> str` | `openbot/application/sandbox_cache_key.py` (Part 1) | All adapters; never exported to handlers |
| `CacheCorruptedError(Exception)` | `openbot/application/sandbox_cache_key.py` (Part 1) | Adapter `_refresh_to_ref` raises; dispatcher catches |
| `NoOpSandboxCache` | `openbot/infrastructure/sandboxes/cache_noop.py` (Part 1) | Default wiring on backends without snapshot support |
| `InMemorySandboxCache` | `openbot/infrastructure/sandboxes/cache_fake.py` (Part 1) | Tests + dev-mode warm pool |
| `DaytonaSnapshotCache` | `openbot/infrastructure/sandboxes/cache_daytona.py` (Part 3) | Production wiring when `OPENBOT_DAYTONA_API_KEY` is set |
| `PreflightContext.sandbox_cache: SandboxCachePort \| None` | `openbot/application/middleware/preflight.py` (Part 2, modify) | Dispatcher only |
| `openbot_sandbox_cache_total` Counter | `openbot/infrastructure/observability/metrics.py` (Part 2) | Dispatcher emit on every acquire |
| `openbot_sandbox_cache_publish_total` Counter | (same) (Part 2) | Dispatcher emit on every publish |
| `openbot_sandbox_cache_acquire_seconds` Histogram | (same) (Part 2) | Dispatcher record from acquire start to handle returned |

If a name in a later part doesn't match this table, **fix the earliest part and re-run its tests** — do not paper over downstream.

---

## Part 1 — Port + key + in-memory cache

**Goal:** All new types, the cache port, and a fully-functional `InMemorySandboxCache` land with full test coverage. Dispatcher is NOT modified yet. Production is unaffected.

**Files:**

| Path | Action |
|---|---|
| `openbot/application/ports/sandbox_cache.py` | NEW |
| `openbot/application/sandbox_cache_key.py` | NEW |
| `openbot/infrastructure/sandboxes/cache_noop.py` | NEW |
| `openbot/infrastructure/sandboxes/cache_fake.py` | NEW |
| `tests/application/ports/test_sandbox_cache_port_contract.py` | NEW (matches the existing `test_<port>_port_contract.py` convention) |
| `tests/application/test_sandbox_cache_key.py` | NEW |
| `tests/infrastructure/sandboxes/test_cache_noop.py` | NEW |
| `tests/infrastructure/sandboxes/test_cache_fake.py` | NEW |

### Task 1.1: `SandboxCachePort` protocol

- [x] **Write failing test** `tests/application/ports/test_sandbox_cache_port_contract.py`:
  - `test_complete_implementer_satisfies_port` — inline `_Complete` dummy with all three coroutines passes `isinstance`. (Use an inline dummy here so Task 1.1 doesn't depend on the not-yet-built `NoOpSandboxCache`; Task 1.3 adds a NoOp-specific isinstance test.)
  - `test_missing_acquire_does_not_satisfy_port`, `test_missing_publish_does_not_satisfy_port`, `test_missing_evict_repo_does_not_satisfy_port` — one test per missing method, for clearer failure messages on regression.
- [x] **Implement** `openbot/application/ports/sandbox_cache.py`:
  - `@runtime_checkable class SandboxCachePort(Protocol)` with `async def acquire`, `async def publish`, `async def evict_repo`.
  - Docstrings match spec § "Type contract" verbatim (acquire returns `SandboxedHandle | None` on miss; backend errors MAY raise).
- [x] Run `make check`. Commit: `feat(application): SandboxCachePort protocol` — landed as `28c20b3` (1097→1101 tests).

### Task 1.2: `_cache_key` + `CacheCorruptedError`

- [x] **Write failing test** `tests/application/test_sandbox_cache_key.py` — 12-test matrix (9 from spec + 3 added at implementation):

  | Test | Inputs | Expected |
  |---|---|---|
  | `test_key_is_deterministic` | same `(checkout, installation_id)` twice | same 24-char hex string |
  | `test_key_varies_on_installation_id` | swap installation_id | different key |
  | `test_key_varies_on_repo_url_case_is_same_key` | `Github.com/X` vs `github.com/x` | **same** key (normalized lowercase) |
  | `test_key_strips_trailing_slash` | URL with `/` vs without | same key |
  | `test_key_varies_on_ref` | swap SHA | different key |
  | `test_key_varies_on_strategy` | SHALLOW vs BLOBLESS | different key |
  | `test_key_is_order_stable_on_sparse_paths` | `("a", "b")` vs `("b", "a")` | same key |
  | `test_key_varies_on_sparse_paths_membership` | `("a",)` vs `("a", "b")` | different key |
  | `test_key_length_is_24_hex_chars` | any valid input | matches `^[0-9a-f]{24}$` |
  | `test_key_ignores_diff_base` *(added)* | swap diff_base | **same** key — diff_base is review-time metadata, not a tree input |
  | `test_key_rejects_token_shaped_repo_url` *(added — cross-cutting security gate)* | repo_url contains `x-access-token:` | raises `TypeError` |
  | `test_cache_corrupted_error_is_exception` *(added)* | `CacheCorruptedError` shape check | `issubclass(..., Exception)` |

- [x] **Implement** `openbot/application/sandbox_cache_key.py`:
  - `_cache_key(checkout, *, installation_id) -> str` per spec § "Cache key derivation".
  - `class CacheCorruptedError(Exception): pass`.
  - Uses `hashlib.sha256`, 24-char hex prefix.
- [x] Run `make check`. Commit: `feat(application): sandbox cache key derivation` — landed as `78c14e2`. (Note: `make check` is currently red on unrelated `test_agent_checkpointer.py` from commit `e0bb06f`; this task's own files pass fmt + lint + tests cleanly.)

### Task 1.3: `NoOpSandboxCache` ✅ (commit `4201223`)

- [x] **Write failing test** `tests/infrastructure/sandboxes/test_cache_noop.py`:
  - `test_noop_acquire_returns_none` — even with a valid `CheckoutSpec`, returns `None`.
  - `test_noop_publish_is_idempotent_noop` — call twice with the same handle; never raises.
  - `test_noop_evict_repo_is_idempotent_noop`.
  - `test_noop_satisfies_port` — `isinstance(NoOpSandboxCache(), SandboxCachePort) is True`.
- [x] **Implement** `openbot/infrastructure/sandboxes/cache_noop.py`:
  - Class implements the three methods; each returns `None` (or does nothing).
  - Module docstring covers the null-object rationale (no `if cache is None` guards at the call site).
  - Also exported from `openbot/infrastructure/sandboxes/__init__.py` alongside `DaytonaSandboxAdapter` / `FakeSandboxAdapter` so wiring imports stay tidy.
- [x] Run targeted tests + lint + import contract; commit as `feat(sandbox-cache): NoOpSandboxCache default backend`.

### Task 1.4: `InMemorySandboxCache` (LRU + TTL warm pool) ✅ (commit `19fab6a`)

- [x] **Write failing test** `tests/infrastructure/sandboxes/test_cache_fake.py` — 9 tests:
  - `test_first_acquire_is_miss`, `test_publish_then_acquire_hits`, `test_acquire_runs_refresh_to_ref`
    (asserts set-url+fetch+reset command sequence via `_ScriptedSandbox` stub)
  - `test_acquire_evicts_and_misses_on_refresh_failure` *(added — corrupted-snapshot eviction)*
  - `test_publish_is_idempotent`, `test_lru_evicts_oldest_when_max_exceeded`, `test_ttl_evicts_stale_entries`
    (TTL=0 instead of sleep — deterministic), `test_evict_repo_clears_keys_for_one_repo_only`,
    `test_concurrent_publish_is_safe`
- [x] **Implement** `openbot/infrastructure/sandboxes/cache_fake.py`:
  - `OrderedDict[str, _Entry]` index; `move_to_end` for O(1) LRU bump; `popitem(last=False)` for eviction.
  - `asyncio.Lock` wraps only index mutations; git I/O runs outside to avoid serialising acquires.
  - `_refresh_to_ref`: set-url → fetch → reset; non-zero exit raises `CacheCorruptedError`.
  - TTL check uses `>=` so `ttl_seconds=0` makes entries immediately stale without `asyncio.sleep`.
  - `_inject_token` handles both HTTPS (inject credential) and `file://` (pass through unchanged) so
    test stubs using local origins work without errors.
  - `size()` test-visible helper; not part of `SandboxCachePort`.
- [x] Pre-commit hooks passed (ruff import sort applied, all green). Commit `19fab6a`.

**Part 1 acceptance:** all four new modules have ≥ 95% line coverage; `make check` green; dispatcher unchanged; no production behavior change.

---

## Part 2 — Dispatcher wiring + observability

**Goal:** `dispatcher._run_with_sandbox` consults `ctx.sandbox_cache` before opening the factory. When the cache is `None` (default), behavior is byte-identical to today. When a cache is wired, hit/miss/stale/error all observable via counters.

**Files:**

| Path | Action |
|---|---|
| `openbot/application/middleware/preflight.py` | MODIFY (+ `sandbox_cache: SandboxCachePort \| None = None` field) |
| `openbot/application/dispatcher.py` | MODIFY (`_run_with_sandbox` consults cache; both entrypoints) |
| `openbot/infrastructure/observability/metrics.py` | MODIFY (+ 3 counters/histograms) |
| `tests/application/middleware/test_preflight.py` | MODIFY (+ default-None field test) |
| `tests/application/test_dispatcher_cache.py` | NEW (integration matrix) |
| `tests/infrastructure/observability/test_metrics.py` | MODIFY (+ counter registration tests) |

### Task 2.1: `PreflightContext.sandbox_cache`

- [ ] **Write failing test** in `tests/application/middleware/test_preflight.py`:
  - `test_preflight_context_sandbox_cache_default_is_none`.
  - `test_replace_sandbox_cache_preserves_other_fields` — confirms `dataclasses.replace` immutability discipline still holds.
- [ ] **Implement** in `openbot/application/middleware/preflight.py`:
  - Add `sandbox_cache: SandboxCachePort | None = None` field.
- [ ] Run `make check`. Commit: `feat(preflight): carry SandboxCachePort on context`.

### Task 2.2: Observability — counters + histogram

- [ ] **Write failing test** `tests/infrastructure/observability/test_metrics.py`:
  - `test_sandbox_cache_total_has_feature_and_result_labels`.
  - `test_sandbox_cache_publish_total_has_feature_and_result_labels`.
  - `test_sandbox_cache_acquire_seconds_has_result_label_and_exponential_buckets`.
- [ ] **Implement** in `openbot/infrastructure/observability/metrics.py`:
  - `sandbox_cache_total = Counter("openbot_sandbox_cache_total", labels=["feature", "result"])`.
  - `sandbox_cache_publish_total = Counter("openbot_sandbox_cache_publish_total", labels=["feature", "result"])`.
  - `sandbox_cache_acquire_seconds = Histogram("openbot_sandbox_cache_acquire_seconds", labels=["result"], buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60))`.
  - Mirror via existing `_LabelledSentryCounter` wrapper.
- [ ] Run `make check`. Commit: `feat(observability): sandbox cache metrics`.

### Task 2.3: `_run_with_sandbox` consults cache

- [ ] Read the current `_run_with_sandbox` (`openbot/application/dispatcher.py` lines ~99–280) to confirm the insertion point: between policy resolution and `async with ctx.sandbox_factory()`.
- [ ] **Write failing test** `tests/application/test_dispatcher_cache.py` — parametrized matrix per spec § "Test strategy":

  | Scenario | `cache.acquire` mock returns | Expected handler `sandbox_handle` | Expected counters |
  |---|---|---|---|
  | miss → handler success | `None` | non-None from factory | `cache_total{result=miss}`, `cache_publish_total{result=created}` |
  | miss → handler raises | `None` | non-None from factory | same as above; publish still attempted in `finally` |
  | hit | `SandboxedHandle(...)` | the hit handle | `cache_total{result=hit}` only |
  | stale (mock `acquire` returns None internally after TTL) | `None` | non-None from factory | `cache_total{result=stale}` (mock signals via additional counter call) |
  | `acquire` raises `BackendError` | raises | non-None from factory; log warning | `cache_total{result=backend_error}` |
  | `ctx.sandbox_cache is None` | (mock not called) | non-None from factory | (no cache metrics) |

  Each row asserts: (a) which `cache` methods were called, (b) which counters fired with which labels, (c) handler invoked with expected `sandbox_handle` identity.

- [ ] **Implement** the cache-aware branch in `_run_with_sandbox`:
  ```python
  cache = ctx.sandbox_cache
  if cache is not None:
      start = time.perf_counter()
      try:
          cached = await cache.acquire(
              checkout, token, installation_id=event.installation_id
          )
      except Exception as exc:
          _logger.warning("sandbox_cache_acquire_failed", extra={"err": str(exc)})
          sandbox_cache_total.labels(feature=feature.value, result="backend_error").inc()
          cached = None
      if cached is not None:
          sandbox_cache_total.labels(feature=feature.value, result="hit").inc()
          sandbox_cache_acquire_seconds.labels(result="hit").observe(
              time.perf_counter() - start
          )
          sandboxed_ctx = dataclasses.replace(
              ctx, sandbox_handle=cached, classifier_output=classifier_output
          )
          await dispatch.handler(sandboxed_ctx)
          return
      # else: fall through to cold path; counter emitted there
  ```
  And on the cold path's exit, schedule async publish:
  ```python
  try:
      await dispatch.handler(sandboxed_ctx)
  finally:
      if cache is not None:
          asyncio.create_task(
              _safe_publish(cache, handle, event.installation_id, feature)
          )
  ```
  Where `_safe_publish` catches all exceptions and increments `cache_publish_total{result=failed}` instead of raising.
- [ ] Replicate the same block in `execute_handler` (worker-side entry point) — both must behave identically.
- [ ] Run `make check`. Commit: `feat(dispatcher): consult SandboxCachePort before opening factory`.

**Part 2 acceptance:** parametrized integration matrix passes; existing dispatcher tests still green; on `ctx.sandbox_cache is None`, *zero* cache metrics emitted (verified by reading the registry pre/post call); fix E2E demo 08 still green (proof of no behavior change).

---

## Part 3 — Daytona snapshot adapter

**Goal:** Production-grade cache backend that uses Daytona's snapshot/template API. All snapshot-format-specific code lives here; nothing leaks out to the application layer.

**Files:**

| Path | Action |
|---|---|
| `openbot/infrastructure/sandboxes/cache_daytona.py` | NEW |
| `tests/infrastructure/sandboxes/test_cache_daytona.py` | NEW |

### Task 3.1: `DaytonaSnapshotCache.acquire` + `_refresh_to_ref`

- [ ] **Write failing test** `tests/infrastructure/sandboxes/test_cache_daytona.py`:
  - Mock the Daytona SDK's `list_snapshots(labels={...})` and `create_workspace(snapshot_id=...)` methods.
  - `test_acquire_miss_when_no_snapshot_matches` — SDK returns empty list → `acquire` returns None + `cache_total{result=miss}`.
  - `test_acquire_hit_calls_hydrate_and_refresh` — SDK returns one snapshot → adapter creates workspace from it → runs `git fetch + git reset --hard <ref>` (assert via mocked `sandbox.run` calls) → returns `SandboxedHandle`.
  - `test_acquire_treats_stale_snapshot_as_miss` — snapshot's `created_at` > TTL → returns None + `cache_total{result=stale}`.
  - `test_acquire_logs_and_evicts_on_corrupted_tree` — `git reset --hard` returns non-zero → raises `CacheCorruptedError` internally, returns None, async-evicts the entry.
  - `test_acquire_treats_backend_error_as_miss_with_log` — SDK raises → returns None + `cache_total{result=backend_error}` + warning log.
  - `test_token_is_injected_into_remote_set_url_not_logged` — `_redact_tokens` covers the `set-url` command output.
- [ ] **Implement** `openbot/infrastructure/sandboxes/cache_daytona.py`:
  - `class DaytonaSnapshotCache(SandboxCachePort)` constructor takes `daytona_client`, `ttl_seconds`, `max_entries`.
  - `acquire` calls `_lookup(key)`, gates on TTL, hydrates, refreshes, returns or returns-None.
  - `_refresh_to_ref` matches spec § "Resolution algorithm" step 6 exactly.
  - Reuse `_inject_token` and `_redact_tokens` from `cache_daytona.py`'s sibling `daytona.py`.
- [ ] Run `make check`. Commit: `feat(sandbox-cache): Daytona acquire + refresh-to-ref`.

### Task 3.2: `DaytonaSnapshotCache.publish` (snapshot creation, idempotent)

- [ ] **Write failing test**:
  - `test_publish_creates_snapshot_with_key_label` — assert SDK `create_snapshot(labels={"openbot_key": ...})` called once.
  - `test_publish_is_idempotent_on_existing_key` — mock `_lookup` to return an existing snapshot → `create_snapshot` NOT called.
  - `test_publish_records_publish_total_created_or_exists` — counter labels match outcome.
  - `test_publish_handles_sdk_failure_with_publish_total_failed` — SDK raises → log + counter; does NOT raise.
  - `test_publish_excludes_secret_class_paths` — assert pre-publish allowlist filter strips `.env*`, `*.pem`, `*.key`, `.doppler/`, `evals/logs/` (per `CLAUDE.md` forbidden list). Implementation: a pre-snapshot `find . -name "<pattern>" -delete` (running inside the sandbox) followed by `git status` assertion that no excluded path is present.
- [ ] **Implement** `publish` per spec § "Publish (snapshot creation)" and § "Snapshot exclusions".
  - Exclusion sweep MUST run before the snapshot call (defence-in-depth on top of `.gitignore`).
- [ ] Run `make check`. Commit: `feat(sandbox-cache): Daytona publish + secret allowlist`.

### Task 3.3: Eviction policies

- [ ] **Write failing test**:
  - `test_lru_eviction_runs_on_publish_when_count_exceeds_max` — fill cache to `max_entries`; publish one more; oldest evicted via `delete_snapshot`.
  - `test_ttl_eviction_runs_on_acquire_for_stale_entry`.
  - `test_evict_repo_deletes_all_keys_for_repo_url` — given 3 cached refs of repo X and 1 of repo Y → `evict_repo("X")` → only Y survives.
- [ ] **Implement** the three eviction policies as helper methods on `DaytonaSnapshotCache`.
  - LRU bookkeeping: snapshot labels include `openbot_last_access` (ISO timestamp); refreshed on each hit. Eviction sorts by this field.
  - Per-repo: filter snapshots by `labels.openbot_repo_url == repo_url`.
- [ ] Run `make check`. Commit: `feat(sandbox-cache): Daytona LRU + TTL + per-repo eviction`.

**Part 3 acceptance:** all `cache_daytona` tests green; SDK calls fully mocked (no network in CI); per-task budget < $0 by construction (no real Daytona consumption).

---

## Part 4 — Rollout, guardrails, E2E

**Goal:** Wire cache into DI behind env flags; add the snapshot-content security audit gate; ship one E2E demo proving the < 1 s hit latency claim. Phase-1 default keeps cache OFF in production.

**Files:**

| Path | Action |
|---|---|
| `openbot/core/dependencies.py` (or wherever DI is) | MODIFY (env-gated cache wiring) |
| `openbot/core/settings.py` | MODIFY (+ 3 settings: enabled, features, max_entries, ttl_seconds) |
| `tests/core/test_dependencies.py` | MODIFY (+ cache wiring tests) |
| `tests/e2e/test_spec_demos.py` | MODIFY (+ demo 12) |
| `tests/e2e/conftest.py` | MODIFY (+ cache fixture for the new demo) |
| `docs/adr/2026-05-XX-sandbox-cache.md` | NEW (optional ADR-style note) |

### Task 4.1: Settings + DI wiring

- [ ] **Write failing test** `tests/core/test_dependencies.py`:
  - `test_cache_disabled_by_default` — `Settings()` → `sandbox_cache_enabled is False` → DI builds `NoOpSandboxCache`.
  - `test_cache_enabled_chat_only_wires_per_feature` — `Settings(sandbox_cache_enabled=True, sandbox_cache_features="chat")` → DI wires `DaytonaSnapshotCache` for chat dispatches and `NoOpSandboxCache` for others.
  - `test_cache_enabled_requires_daytona_backend` — `Settings(sandbox_cache_enabled=True)` with `OPENBOT_SANDBOX_BACKEND=docker` raises `ConfigurationError`.
- [ ] **Implement** in `openbot/core/settings.py`:
  - `sandbox_cache_enabled: bool = False`.
  - `sandbox_cache_features: tuple[str, ...] = ()` (parsed from comma-separated env).
  - `sandbox_cache_max_entries: int = 50`.
  - `sandbox_cache_ttl_seconds: int = 86_400`.
- [ ] **Implement** DI wiring per spec § "Migration plan" Phase 1 — `NoOpSandboxCache` is the default; `DaytonaSnapshotCache` activates only when env-enabled AND feature matches.
- [ ] Run `make check`. Commit: `feat(config): env-gated sandbox cache wiring`.

### Task 4.2: Snapshot security audit gate

- [ ] **Write failing test** `tests/infrastructure/sandboxes/test_cache_daytona.py`:
  - `test_publish_runs_secret_scan_pre_snapshot` — mock a workspace containing a `.env` file → `publish` deletes it before calling `create_snapshot`; assert deletion happened.
  - `test_publish_aborts_if_post_sweep_still_finds_secrets` — second sweep finds `secret.pem` → `publish` aborts + log + `cache_publish_total{result=failed}` increments.
- [ ] **Implement** the two-pass sweep in `DaytonaSnapshotCache.publish`:
  - Pass 1: delete known patterns (defence-in-depth on `.gitignore`).
  - Pass 2: re-scan; if any pattern still matches → abort.
  - Patterns: `.env*`, `*.pem`, `*.key`, `.doppler/`, `evals/logs/`, `.langgraph/`, `.inspect/` (mirror the `CLAUDE.md` forbidden list).
- [ ] Run `make check`. Commit: `feat(sandbox-cache): two-pass secret allowlist before snapshot`.

### Task 4.3: E2E demo `test_demo_12_chat_cache_hit_under_one_second`

- [ ] **Write failing test** `tests/e2e/test_spec_demos.py::test_demo_12_chat_cache_hit_under_one_second`:
  - Fixture sets `OPENBOT_SANDBOX_CACHE_ENABLED=true`, `OPENBOT_SANDBOX_CACHE_FEATURES=chat`.
  - First chat event: posts a question on a PR; expected to take cold-path duration.
  - Second chat event: same `(repo, ref)`; assert response time < 1 s wall-clock.
  - Assert second event's `sandbox_cache_total{feature=chat,result=hit}` counter delta = 1.
  - Marked `@pytest.mark.skip(reason="env-gated; opt-in")` unless `RUN_CACHE_E2E=1`.
- [ ] **Update** `tests/e2e/conftest.py` to expose a cache fixture (uses `InMemorySandboxCache` to keep CI hermetic — Daytona-backed E2E lives in a separate `tests/e2e_daytona/` suite only run on opt-in).
- [ ] Run `make check`. Run E2E demo 12 locally with `RUN_CACHE_E2E=1`. Commit: `feat(e2e): demo 12 — chat cache hit < 1s P95`.

### Task 4.4: ADR note (optional)

- [ ] Write `docs/adr/2026-05-XX-sandbox-cache.md` capturing: why a port, why per-feature env gating, why two-pass secret sweep. ~150 lines.
- [ ] Commit: `docs(adr): sandbox snapshot cache architecture`.

**Part 4 acceptance:** cache disabled by default; demo 12 passes under env-flag; settings validation rejects invalid backend combinations.

---

## Cross-cutting concerns

### Security

- [ ] Verify `_redact_tokens` covers the new `git remote set-url` command output (the auth URL gets logged on failure paths).
- [ ] Confirm no token-typed value is reachable from `_cache_key` inputs (the token isn't a key component by design).
- [ ] Add regression test: assert `_cache_key` raises `TypeError` if anyone tries to pass a token-shaped string as `repo_url` (heuristic: contains `x-access-token:`).
- [ ] Manual snapshot-content audit before Phase 2 enablement (per spec acceptance).

### Operability

- [ ] Sentry alert wiring: `CacheCorruptedError` rate > 1/min and `cache_total{result=backend_error}` rate > 5/min surface as warnings; both link to runbook.
- [ ] Runbook fragment: `docs/runbooks/sandbox-cache.md` describing how to flip the env back to `false`, what hit-rate to expect (target ≥ 50% after warmup), how to interpret each `result` label.

### Documentation

- [ ] Update parent unified-sandbox-entry plan's status checkpoint when each Part lands (mirror the existing pattern).
- [ ] When Phase 4 rollout completes, archive this spec + plan + the parent plan to `docs/_archive/superpowers/` per `CLAUDE.md` rule.

### Eval suite

- [ ] No eval surface for this slice — perf optimization, no LLM behavior change.

---

## Risk-aware ordering

If any part is delayed:
- **Part 1 alone** is harmless (dead code; no production reach).
- **Part 2 without Part 3** is also harmless because the default DI wires `NoOpSandboxCache` — no cache code path runs in production.
- **Parts 1 + 2 + 3 without Part 4** is harmless because the env gate keeps the cache off.
- **Part 4 enablement** is the only step that touches production behavior; it's reversible by flipping the env back to `false`.

Each part is independently revertable. The full slice can be paused at any boundary without leaving partial state in production.

---

## Acceptance for the whole slice

- [ ] All 4 parts merged.
- [ ] `make check` green at every commit.
- [ ] Hexagonal contract held (lint-imports clean throughout).
- [ ] E2E demo 12 passes locally with `RUN_CACHE_E2E=1`.
- [ ] Cache disabled by default in production deployment config.
- [ ] One manual snapshot-content audit completed before any Phase 2 enablement.
- [ ] Spec + plan + parent unified-sandbox-entry plan archived to `docs/_archive/superpowers/` once Phase 4 enablement is complete and stable for ≥ 1 week.

---

## Why this plan over the alternatives

### Alternative: One PR with all 4 parts

Coupling provisioning + Daytona-specific snapshot mechanics + rollout into a single PR makes review brutal and bisect useless. Splitting at hexagonal boundaries (port → wiring → adapter → rollout) gives each PR a single concern and each commit a single revert point.

### Alternative: Skip Part 1's `InMemorySandboxCache` (only ship `NoOp` + `Daytona`)

The in-memory cache is what makes dispatcher tests fast and Daytona-free. Without it, every cache-aware test would mock the Daytona SDK individually; with it, tests just plug in `InMemorySandboxCache` and exercise the real port semantics. The 1 hour of investment in Part 1.4 pays back in every subsequent test.

### Alternative: Land Daytona adapter before dispatcher wiring

Tempting because the adapter "feels production-relevant" earlier, but the dispatcher wiring is where the *interface contract* gets validated. Without Part 2's integration matrix locking the call shape, Part 3's adapter could drift from what the dispatcher actually needs. Hexagonal discipline says: ports first, adapters second.
