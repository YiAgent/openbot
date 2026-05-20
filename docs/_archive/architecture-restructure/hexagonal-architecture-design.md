# OpenBot Hexagonal Restructure — Design

**Date**: 2026-05-18
**Status**: Draft — pending user review
**Scope**: Reorganize `openbot/` package from flat layout to strict Hexagonal / Ports-Adapters

---

## 1. Problem

Today the `openbot/` package mixes seven top-level modules (`webapp.py`, `router.py`, `dispatch.py`, `events.py`, `config.py`, `config_repo.py`, `obs.py`, `setup_wizard.py`) with eight well-bounded sub-packages (`adapters/`, `handlers/`, `llm/`, `middleware/`, `persistence/`, `queue/`, `state/`, `workflows/`). The mix has three concrete symptoms:

1. **`webapp.py` is 541 lines** — FastAPI app + lifespan + route handlers + state-machine orchestration + auth construction all collapsed into one file.
2. **The web↔worker shared spine is obscured** — `dispatch.py` is deliberately reused by both `webapp.py` and `queue/worker.py` (per harness spec §3 M3 "single source of truth for middleware chain order"), but the current layout doesn't telegraph this.
3. **DeepAgents is about to land** — workflows are still ACK-only stubs; the real agent runtime needs a clean seam so it can be swapped (DeepAgents → LangGraph → …) without rewriting workflow business logic.

## 2. Constraints (locked)

- **Two process entrypoints stay**: `web: uvicorn openbot.entrypoints.api.app:app` and `worker: python -m openbot.infrastructure.queue.runner` (per `Procfile`). One CLI entrypoint (`setup_wizard`) joins them.
- **PRD §3 sandbox boundary is locked**: `evals.sandboxes.factory` stays under `evals/`, not under `openbot/`. This restructure does not touch evals.
- **Harness spec §3 M3 invariant**: web and worker MUST share the same middleware chain via a single module. The new `application/dispatcher.py` keeps this contract.
- **PRD §4 terminology**: docs use "triage workflow", "review workflow", etc. The package keeps `workflows/` (not `use_cases/`) to preserve doc references.
- **Test cost is acknowledged**: ~60 test files mirror current sub-packages and will need import updates. This is accepted as one-shot migration cost.

## 3. Target Architecture

Strict Hexagonal with four layers:

```
entrypoints/  ──►  application/  ──►  domain/
                          ▲
                          │ depends on Ports (Protocols)
                          │
                  infrastructure/  ──►  domain/
                       (implements Ports)
```

### Dependency rules

| Layer            | May import from           | Must NOT import           |
|------------------|---------------------------|---------------------------|
| `domain/`        | stdlib + `pydantic`       | application, infra, entrypoints |
| `application/`   | `domain/` + own ports     | `infrastructure/` directly      |
| `infrastructure/`| `domain/` + own ports     | `application/`, `entrypoints/`  |
| `core/`          | stdlib + `pydantic-settings` | application, infra, entrypoints |
| `entrypoints/`   | all four layers           | — (this is the composition root) |

`infrastructure/` modules implement Protocols defined in `application/ports/` — they import the Protocol for type-checking only (under `TYPE_CHECKING`) to keep the arrow direction clean.

## 4. Target Directory Tree

```
openbot/
├── domain/
│   ├── __init__.py
│   ├── events.py                # UnifiedEvent, EventKind  (was: events.py)
│   ├── intents.py               # Intent enum             (was: state/intents.py)
│   ├── identifiers.py           # derive_run_id, task_id  (was: nested in router.py)
│   └── config_schema.py         # EffectiveConfig dataclass (extracted from config_repo.py)
│
├── application/
│   ├── __init__.py
│   ├── ports/                   # All Protocol interfaces (11 protocols)
│   │   ├── __init__.py
│   │   ├── channel_adapter.py   # ChannelAdapterPort     (reply, label, set_role)
│   │   ├── config_loader.py     # ConfigLoaderPort       (load_for_repo)
│   │   ├── audit_log.py         # AuditLogPort
│   │   ├── runs_repo.py         # RunsRepoPort           (transition, etc.)
│   │   ├── resource_lock.py     # ResourceLockPort
│   │   ├── cancellation.py      # CancellationPort
│   │   ├── dedup.py             # DedupPort              (WebhookDedup behavior)
│   │   ├── queue.py             # QueuePort              (enqueue)
│   │   ├── rate_limiter.py      # RateLimiterPort
│   │   ├── llm.py               # LLMPort                (completion)
│   │   └── sandbox.py           # SandboxPort            (agent runtime)
│   ├── router.py                # event → Dispatch              (was: router.py)
│   ├── dispatcher.py            # web+worker shared spine       (was: dispatch.py)
│   ├── workflows/               # PRD §4 feature entry points   (was: workflows/)
│   │   ├── __init__.py
│   │   ├── _lifecycle.py
│   │   ├── triage.py
│   │   ├── review.py
│   │   ├── fix.py
│   │   ├── chat.py
│   │   └── chat_parser.py
│   ├── agents/                  # Business-side agent orchestration
│   │   └── __init__.py          # prompts / tool lists / safety policy — populated when agents land
│   ├── middleware/              # Pre-flight chain               (was: middleware/)
│   │   ├── __init__.py
│   │   ├── preflight.py
│   │   ├── audit.py
│   │   ├── budget.py
│   │   ├── cancel.py
│   │   ├── feature_toggle.py
│   │   ├── rate_limit.py
│   │   ├── sanitize.py
│   │   └── security.py
│   ├── handlers/                # Composed handlers             (was: handlers/)
│   │   └── debug_echo.py
│   └── state/                   # State machine logic           (was: state/)
│       ├── __init__.py
│       ├── classifier.py
│       ├── runs_repo.py
│       ├── resource_lock.py
│       └── cancellation.py
│
├── infrastructure/
│   ├── __init__.py
│   ├── adapters/                # Channel adapters (was: adapters/)
│   │   ├── base.py              # ABC kept; satisfies ChannelAdapterPort
│   │   ├── github.py
│   │   └── github_auth.py
│   ├── persistence/             # DB + Redis primitives (was: persistence/)
│   │   ├── db.py                # SQLAlchemy engine/session factory
│   │   ├── redis.py             # Redis client factory
│   │   ├── models.py            # ORM models (Workflow, TaskRun, AuditLog, CostMeter)
│   │   ├── dedup_repo.py        # implements DedupPort       (was: dedup.py)
│   │   ├── audit_log_repo.py    # implements AuditLogPort
│   │   ├── runs_repo_impl.py    # implements RunsRepoPort
│   │   └── rate_limiter_redis.py # implements RateLimiterPort
│   ├── queue/                   # (was: queue/)
│   │   ├── enqueue.py           # implements QueuePort
│   │   ├── payload.py
│   │   └── worker.py            # Redis Stream consumer
│   ├── llm/                     # (was: llm/)
│   │   ├── complete.py          # implements LLMPort
│   │   ├── router.py            # Feature enum (rename to model_router.py to avoid name collision)
│   │   └── sanitize.py
│   ├── agents/                  # DeepAgents SDK adapter
│   │   └── __init__.py          # populated when agents land — implements SandboxPort
│   ├── config_loader.py         # implements ConfigLoaderPort (was: config_repo.py I/O parts)
│   └── observability.py         # Sentry init                 (was: obs.py)
│
├── core/                        # Cross-cutting, no business logic
│   ├── __init__.py
│   ├── settings.py              # Pydantic Settings           (was: config.py)
│   └── logging.py               # logger config (new — extracted from scattered logger setup)
│
└── entrypoints/                 # Composition roots
    ├── __init__.py
    ├── api/                     # FastAPI process
    │   ├── __init__.py
    │   ├── app.py               # FastAPI() + lifespan        (was: webapp.py shell)
    │   ├── deps.py              # FastAPI Depends factories — wires ports → infra impls
    │   └── routes/
    │       ├── __init__.py
    │       ├── health.py        # GET /health                 (was: in webapp.py)
    │       └── github_webhook.py # POST /webhook/github       (was: in webapp.py)
    ├── worker/                  # Redis Stream process
    │   ├── __init__.py
    │   └── __main__.py          # `python -m openbot.entrypoints.worker` (was: queue/runner.py)
    └── cli/                     # Command-line tools
        ├── __init__.py
        └── setup_wizard.py      # (was: setup_wizard.py)
```

### Procfile change

```diff
- web: uvicorn openbot.entrypoints.api.app:app --host 0.0.0.0 --port $PORT ...
- worker: python -m openbot.infrastructure.queue.runner
+ web: uvicorn openbot.entrypoints.api.app:app --host 0.0.0.0 --port $PORT ...
+ worker: python -m openbot.entrypoints.worker
```

### Makefile change

```diff
- APP ?= openbot.entrypoints.api.app:app
+ APP ?= openbot.entrypoints.api.app:app
```

## 5. Port Catalogue (11 protocols)

| Port (`application/ports/`)   | Implemented by (`infrastructure/`)           | Replaces today's direct dependency on |
|-------------------------------|----------------------------------------------|----------------------------------------|
| `ChannelAdapterPort`          | `adapters/github.py` (`GitHubAdapter`)       | `GitHubAdapter` concrete class         |
| `ConfigLoaderPort`            | `config_loader.py`                           | `config_repo.load_for_repo` function   |
| `AuditLogPort`                | `persistence/audit_log_repo.py`              | `AuditLogRepo` concrete class          |
| `RunsRepoPort`                | `persistence/runs_repo_impl.py`              | `state.runs_repo.transition` function  |
| `ResourceLockPort`            | `persistence/redis.py` Lua-script wrapper    | `state.resource_lock.resource_lock` CM |
| `CancellationPort`            | `persistence/redis.py` (pub/sub channel)     | `state.cancellation.signal` function   |
| `DedupPort`                   | `persistence/dedup_repo.py`                  | `WebhookDedup` concrete class          |
| `QueuePort`                   | `queue/enqueue.py`                           | `enqueue` function                     |
| `RateLimiterPort`             | `persistence/rate_limiter_redis.py`          | inline `redis.zadd` calls in middleware |
| `LLMPort`                     | `llm/complete.py`                            | `litellm.acompletion` direct call      |
| `SandboxPort`                 | `agents/*` (DeepAgents + Daytona/Modal/Docker) | placeholder for v0.1 — wired with agents |

Each Port is a `typing.Protocol` with `@runtime_checkable` only where tests need it (most don't — static type-checking suffices).

## 6. Composition Root Pattern

### `entrypoints/api/deps.py`

```python
# Each Depends factory returns an infra impl typed as the Port.
# This is the ONLY place infra is imported by name in the api process.

from fastapi import Depends
from openbot.application.ports.dedup import DedupPort
from openbot.application.ports.queue import QueuePort
from openbot.infrastructure.persistence.dedup_repo import WebhookDedup
from openbot.infrastructure.queue.enqueue import RedisStreamQueue
# ... etc.

def get_dedup(...) -> DedupPort:
    return WebhookDedup(redis=...)

def get_queue(...) -> QueuePort:
    return RedisStreamQueue(redis=...)
```

### `entrypoints/worker/__main__.py`

Worker uses plain function composition (no FastAPI DI):

```python
async def main() -> None:
    settings = get_settings()
    redis = await make_redis_client(settings)
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    # Build all infra impls
    dedup = WebhookDedup(redis)
    audit = AuditLogRepo(session_factory)
    # ... etc.

    # Hand the bundle to application's dispatcher
    await run_worker_loop(redis=redis, dispatcher_deps=DispatcherDeps(
        dedup=dedup, audit=audit, ...
    ))
```

`DispatcherDeps` is a frozen dataclass in `application/dispatcher.py` carrying every Port the chain needs. Middleware constructors take Ports, not infra concrete types.

## 7. Test Migration Strategy

Tests today mirror `openbot/` sub-packages — `tests/middleware/`, `tests/state/`, etc. The migration:

1. **Mirror the new tree**: `tests/domain/`, `tests/application/middleware/`, `tests/infrastructure/persistence/`, etc.
2. **Mechanical sed pass**: `from openbot.application.middleware.X` → `from openbot.application.middleware.X`. Run once across all test files.
3. **Add port-based test doubles**: For each Port, provide a `FakeDedup`, `FakeAudit`, etc. in `tests/_fakes/`. Existing tests that monkey-patch concrete classes get the option to switch to fakes — but the migration does NOT force this rewrite; we accept mixed style for v0.1 and tighten in v0.2.
4. **Entrypoint smoke tests**: Add `tests/entrypoints/api/test_app_boot.py` and `tests/entrypoints/worker/test_main_boot.py` that verify each process imports cleanly with all DI wiring resolved.

## 8. Migration Phases

This spec describes the **target state**. The detailed phased implementation plan is the responsibility of the `writing-plans` step that follows. As a sanity check, the phases will roughly be:

1. **Phase 1 — Skeleton + moves**: create the new directories, move files (`git mv` to preserve history), update imports mechanically. No behavior change. CI green.
2. **Phase 2 — Ports & DI**: introduce Protocol files, refactor `infrastructure/` modules to implement them, wire `entrypoints/api/deps.py` and `entrypoints/worker/__main__.py` as composition roots. Middleware constructors switch to Port type hints.
3. **Phase 3 — Test alignment**: mirror `tests/` to the new layout, add port-based fakes, add entrypoint smoke tests.
4. **Phase 4 — Procfile + Makefile + docs**: flip process entry strings, update `make help`, refresh CLAUDE.md / PRD §3 module references.

Each phase is one PR; CI must stay green between phases.

## 9. Out of Scope

- **`evals/` package**: untouched. Sandbox factory boundary (`evals.sandboxes.factory`) is PRD-locked.
- **Database schema**: no Alembic migration. ORM models simply move package paths.
- **Public API**: there is no public Python API yet, so the rename has no external impact.
- **Logging contract**: log keys like `"debug_echo"`, audit_log payload schemas, Sentry tags stay byte-identical.

## 10. Risks

| Risk | Mitigation |
|------|------------|
| Heroku deploy breaks on Procfile mismatch | Phase 4 flips Procfile last; staging deploy verifies before prod promotion. |
| Circular import surface area increases | Strict layer rules + lint (e.g., `import-linter`) added in Phase 2. |
| Test mass-rename produces silent fixture mismatches | Run `make test` after Phase 1's mechanical sed; treat any failure as a blocker before Phase 2 starts. |
| `OPENBOT_DEBUG_ECHO=1` Heroku path silently regresses | Phase 1 keeps `handlers/debug_echo.py` byte-identical aside from imports; e2e smoke after each phase. |
| `application/middleware` ↔ `infrastructure/persistence` accidental tight coupling reintroduced | Add `import-linter` rule blocking `application.* → infrastructure.*` imports. Violations fail CI. |

## 11. Acceptance Criteria

- [ ] `make check` (= `make fmt-check lint test`) green on the new tree.
- [ ] `uvicorn openbot.entrypoints.api.app:app` boots locally and serves `/health`.
- [ ] `python -m openbot.entrypoints.worker` boots and pulls from the Redis Stream group.
- [ ] `python -m openbot.entrypoints.cli.setup_wizard` runs interactively.
- [ ] `import-linter` (or equivalent) enforces the four-layer arrow rule.
- [ ] Existing 543 tests pass; no test deleted, only relocated and re-imported.
- [ ] `OPENBOT_DEBUG_ECHO=1` end-to-end webhook trace still emits the three sinks (GitHub comment, JSON log, audit row).
- [ ] CLAUDE.md and PRD §3 module references updated to new paths.

---

**Next step**: hand this design to `writing-plans` to produce the phased implementation plan with per-PR file lists, sed scripts, and verification commands.
