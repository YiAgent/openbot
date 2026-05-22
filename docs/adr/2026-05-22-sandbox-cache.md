# ADR: Sandbox Snapshot Cache Architecture

**Date:** 2026-05-22
**Status:** Accepted
**Branch:** `feat/unified-sandbox-entry`
**Related plan:** `docs/superpowers/plans/2026-05-21-sandbox-snapshot-cache-plan.md`

---

## Context

The OpenBot agent sandbox cold-start (Daytona workspace create + git clone + install) takes
10–15 s P95. For repeat events on the same `(installation_id, repo_url, ref)` — common
in active review or chat sessions — this is dead overhead: the repository state hasn't
changed.

## Decision

Insert a `SandboxCachePort` between `dispatcher._run_with_sandbox` and the cold-path
sandbox factory. On a cache hit, the dispatcher skips the factory entirely and runs the
handler immediately with the warm handle.

---

## Why a port (not a concrete class)?

The dispatcher must stay decoupled from the Daytona SDK. Plugging in `InMemorySandboxCache`
for dev/tests and `DaytonaSnapshotCache` for production without touching the dispatcher is
the core value. The `SandboxCachePort` protocol is `@runtime_checkable` so infra adapters
can be validated at startup without importing the domain.

## Why per-feature env gating?

`OPENBOT_SANDBOX_CACHE_FEATURES=chat,fix` lets ops enable the cache on a feature-by-feature
basis. This matters because:

1. **Risk isolation** — a cache regression on `triage` (lowest user-facing impact) should
   not block `fix` (highest user-facing impact) from staying fast.
2. **Gradual rollout** — the cache is an optimization layer, not a correctness layer. Cold
   path is always reachable via `OPENBOT_SANDBOX_CACHE_ENABLED=false`.
3. **pydantic-settings 2.x compatibility** — complex types (tuple/list) from env vars
   trigger JSON parsing before field validators. Storing as a raw `str` field and parsing in
   the factory (`build_sandbox_cache`) sidesteps this, keeping the Settings model simple.

## Why a two-pass secret sweep?

The snapshot API captures the live workspace filesystem regardless of `.gitignore`. A single
pass (`find . -name *.pem -delete`) is not sufficient because:

- The `find -exec rm -rf {} +` invocation on nested directories uses `+` batching, which
  breaks `find`'s `-delete` semantics on POSIX implementations (the `{}` placeholder appears
  once at the end, not per-file). Using `rm -rf` in the exec is a pragmatic workaround.
- A race window exists between the sweep and the snapshot API call.

Pass 2 (verify) re-scans the same patterns without `-delete`. If any result is non-empty,
`_sweep_secrets` raises `RuntimeError` and the `DaytonaSnapshotCache.publish` path aborts
immediately, ensuring no snapshot is created with credential-bearing content.

The forbidden patterns (`_SWEEP_FILE_PATTERNS`, `_SWEEP_DIR_NAMES`, `_SWEEP_DIR_PATHS`)
mirror the list in `CLAUDE.md § Forbidden` and the `detect private key` pre-commit hook.

## Why build_sandbox_cache lives in openbot.application?

The original plan placed the factory in `openbot.core.dependencies`. However, `openbot.core`
is at the bottom of the hexagonal layer contract (below `domain`, below `application` and
`infrastructure`). It cannot import from `openbot.infrastructure` without violating
`lint-imports`.

`openbot.application.sandbox_cache_deps` is the correct home: the application layer is
allowed to import from both `infrastructure` (concrete adapters) and `core` (settings types),
and this is where all other dependency wiring (dispatcher, middleware, preflight chain) lives.

## Token safety

Installation tokens are never stored in snapshots or cache index keys. The `_cache_key`
function derives a SHA-256 hash from `(installation_id, repo_url, ref, strategy)` only.
Tokens are injected at acquire-time via `_refresh_to_ref` (which calls
`git remote set-url` with the fresh token) and at cold-path clone-time via the sandbox
adapter's `_inject_token`. This means:

- A stale snapshot never carries credentials.
- A cache eviction never leaks tokens.
- The token parameter is not a key component by design.

## Observability

Cache outcomes are recorded via three Prometheus metrics wrapped in `_LabelledSentryCounter`
(same pattern as the existing `dispatch_sandbox_total`):

| Metric | Labels | Meaning |
|---|---|---|
| `sandbox_cache_total` | `feature`, `result` | Per-feature hit/miss/backend_error rate |
| `sandbox_cache_publish_total` | `feature`, `result` | Publish created/failed rate |
| `sandbox_cache_acquire_seconds` | `result` | Hit path latency histogram |

All three are emitted from `dispatcher._run_with_sandbox`, keeping the cache adapters
free of metrics concerns.

## Consequences

- Cold path is unchanged and always reachable (`sandbox_cache_enabled=false` default).
- Cache hit skips `~10–15 s` sandbox cold-start on repeat events.
- Snapshot security is defense-in-depth: `.gitignore` + two-pass sweep + verify before snapshot.
- `lint-imports` stays green — `openbot.core` never imports from upper layers.
- Phase 2 (Daytona snapshot-backed cache) is wired but gated; ops opts in via env.

---

*This ADR was written after Tasks 1.1–4.4 of the sandbox snapshot cache plan landed. The
implementation decisions above match the code as committed on `feat/unified-sandbox-entry`.*
