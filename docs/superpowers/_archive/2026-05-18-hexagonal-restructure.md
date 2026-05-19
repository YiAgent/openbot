# OpenBot Hexagonal Restructure — Implementation Plan (Index)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the `openbot/` package from a flat layout to strict Hexagonal / Ports-Adapters with `domain / application / infrastructure / core / entrypoints` layers, 11 explicit Ports, and composition roots in `entrypoints/`.

**Architecture:** Four-layer Hexagonal. `domain` is pure data. `application` holds router / dispatcher / workflows / middleware / state and defines Ports as `typing.Protocol`s. `infrastructure` provides concrete Redis / DB / GitHub / LLM / DeepAgents adapters that implement those Ports. `entrypoints/{api,worker,cli}` are the composition roots — the only places that wire infra into application.

**Tech Stack:** FastAPI · SQLAlchemy 2.x async · Redis · pydantic-settings · `import-linter` (new) · pytest · uv

**Spec:** `docs/superpowers/specs/2026-05-18-hexagonal-restructure-design.md`

---

## Phase Index

The plan is split across four phase files. Execute them **in order**, each as its own PR. CI must stay green between phases.

| Phase | File | Tasks | Goal |
|-------|------|-------|------|
| 1a | [`2026-05-18-hexagonal-restructure-phase-1a-moves.md`](2026-05-18-hexagonal-restructure-phase-1a-moves.md) | 1.1 – 1.6 | Skeleton + move sub-packages and top-level helpers, with shims |
| 1b | [`2026-05-18-hexagonal-restructure-phase-1b-entrypoints-cleanup.md`](2026-05-18-hexagonal-restructure-phase-1b-entrypoints-cleanup.md) | 1.7 – 1.11 | Split `webapp.py`, move worker + CLI, add import-linter, delete all shims |
| 2a | [`2026-05-18-hexagonal-restructure-phase-2a-ports-core.md`](2026-05-18-hexagonal-restructure-phase-2a-ports-core.md) | 2.0 – 2.3 | DispatcherDeps + Dedup, Queue, Channel Ports |
| 2b | [`2026-05-18-hexagonal-restructure-phase-2b-state-ports.md`](2026-05-18-hexagonal-restructure-phase-2b-state-ports.md) | 2.4 – 2.7 | RunsRepo, ResourceLock, Cancellation, AuditLog Ports |
| 2c | [`2026-05-18-hexagonal-restructure-phase-2c-ports-rest.md`](2026-05-18-hexagonal-restructure-phase-2c-ports-rest.md) | 2.8 – 2.11 | RateLimiter, ConfigLoader, LLM, Sandbox Ports + empty ignore list |
| 3 | [`2026-05-18-hexagonal-restructure-phase-3-tests.md`](2026-05-18-hexagonal-restructure-phase-3-tests.md) | 3.1 – 3.2 | Mirror `tests/` to new layout; add entrypoint boot smoke |
| 4 | [`2026-05-18-hexagonal-restructure-phase-4-deploy.md`](2026-05-18-hexagonal-restructure-phase-4-deploy.md) | 4.1 – 4.3 | Flip Procfile / Makefile / docs; staging deploy verification |

---

## Preconditions (run before Phase 1 Task 1.1)

The working tree must be clean OR carry ONLY changes the operator is willing to bundle into Phase 1's first commit. The current index already shows ~30 unrelated staged renames from prior session work — those must be committed or stashed first.

- [ ] **Step 1: Inspect the working tree**

```bash
git status --short
```

- [ ] **Step 2: Stash or commit any unrelated changes**

```bash
# Option A — stash:
git stash push -u -m "pre-restructure-state"

# Option B — commit on the current branch with a separate message:
# (skip if no unrelated changes)
```

- [ ] **Step 3: Confirm the baseline is green**

```bash
make check
```
Expected: 543 passed (per session memory 2026-05-18). If `make check` fails on the current tree, fix that first — the restructure relies on a green baseline to detect regressions.

- [ ] **Step 4: Confirm `uv sync` has run recently**

```bash
uv sync --dev
```

If you intend to do the work in an isolated worktree (recommended for a 4-PR series), see the `superpowers:using-git-worktrees` skill.

---

## File Structure Summary

The new layout (full detail in spec §4):

```
openbot/
├── domain/               { events, intents, identifiers, config_schema }
├── application/
│   ├── ports/            { 11 Protocol files }
│   ├── router.py · dispatcher.py
│   ├── workflows/ · agents/ · middleware/ · handlers/ · state/
├── infrastructure/
│   ├── adapters/ · persistence/ · queue/ · llm/ · agents/
│   └── config_loader.py · observability.py
├── core/                 { settings, logging }
└── entrypoints/
    ├── api/              { app, deps, routes/ }
    ├── worker/           { __main__ }
    └── cli/              { setup_wizard }
```

---

## Cross-Phase Conventions

These conventions apply uniformly across all four phase files.

### Commit granularity

Each numbered Task ends with a `git commit`. Each commit is a self-contained, CI-green checkpoint. Subagent-driven execution should pause for review at every task boundary; inline execution should checkpoint at least at every Phase boundary.

### Shim policy (Phase 1 only)

While files are migrating in Phase 1 (Tasks 1.2 – 1.9), the original path is replaced with a one-line re-export shim so all 543 tests keep passing across each commit. Task 1.11 deletes every shim in one sweep after a bulk import-rewrite. **No shim survives the end of Phase 1.**

### Layer rules (enforced from Task 1.10 onwards)

| Layer            | May import from                  | Must NOT import                |
|------------------|----------------------------------|--------------------------------|
| `domain/`        | stdlib + `pydantic`              | application, infra, entrypoints |
| `application/`   | `domain/` + own `ports/`         | `infrastructure/` directly      |
| `infrastructure/`| `domain/` + own `ports/`         | `application/`, `entrypoints/`  |
| `core/`          | stdlib + `pydantic-settings`     | application, infra, entrypoints |
| `entrypoints/`   | all four layers                  | — (composition root)            |

`infrastructure/` modules implement Protocols defined in `application/ports/` — they import the Protocol for `TYPE_CHECKING` only.

Phase 1's `.importlinter` config (Task 1.10) seeds an `ignore_imports` allowlist for every cross-layer leak that exists at end-of-Phase-1. Each Phase 2 Port task deletes one or more entries until the allowlist is empty.

### Test discipline

- Phase 1 must not change test count. If `make test` reports fewer passes after a Task, the rewrite missed an import path.
- Phase 2 ADDS one contract test per Port (11 new tests).
- Phase 3 ADDS 3 boot smoke tests (api, worker, cli).
- No existing test is deleted at any point.

### Verification gate per Task

Every Task ends with `make check` (= `make fmt-check lint test`, and from Task 1.10 onwards also `lint-imports`). A Task is not done until `make check` is green.

---

## Self-Review Notes

This plan covers every requirement in `docs/superpowers/specs/2026-05-18-hexagonal-restructure-design.md`:

| Spec section | Plan location |
|--------------|---------------|
| §3 architecture & dependency rules | Phase 1b Task 1.10 (`import-linter`) |
| §4 target directory tree           | Phase 1a Tasks 1.1 – 1.6 + Phase 1b Tasks 1.7 – 1.11 |
| §5 11 Ports catalogue              | Phase 2a Tasks 2.1 – 2.3 + Phase 2b Tasks 2.4 – 2.7 + Phase 2c Tasks 2.8 – 2.11 |
| §6 composition root pattern        | Phase 2a Task 2.0 (api `deps.py` + worker `__main__.py`) |
| §7 test migration                  | Phase 3 |
| §8 phasing                         | 1:1 with this plan's Phase 1–4 |
| §11 acceptance criteria            | end-of-phase checklists + Phase 4 Task 4.3 (staging deploy) |

Known scope choices this plan locks in (none of these contradict the spec, they are clarifications):

- `DedupOutcome` is **not** moved to `domain/` in Phase 2 — it stays in `infrastructure/persistence/dedup.py` as a leaf dataclass. Spec §5 allows infra-owned leaf types.
- `infrastructure/llm/router.py` is renamed `model_router.py` (Phase 1 Task 1.4) to avoid name collision with `application/router.py`.
- `LLMPort` (Task 2.10) and `SandboxPort` (Task 2.11) are defined but have no Phase-2 consumer. They exist as a contract so the future agent-slice plugs in cleanly without rewriting middleware.
- `application/agents/` and `infrastructure/agents/` are created in Phase 1 as empty packages and remain placeholders until the agent-slice lands.
