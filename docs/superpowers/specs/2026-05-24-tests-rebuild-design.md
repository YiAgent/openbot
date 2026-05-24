# Tests Rebuild — Six-Layer Engineering Design

**Date:** 2026-05-24
**Author:** brainstorming session, openbot maintainers
**Status:** Draft → pending user review
**Branch target:** `refactor/tests-rebuild` (new, off `main` after current branch lands)

## 1 · Background

`tests/` has grown to 175 files (vs 130 source files in `openbot/`) without a
shared organising principle. Symptoms accumulating in the current tree:

- Two parallel "state" trees: `tests/state/` and `tests/state_machine/` with
  overlapping responsibilities.
- Two parallel "dispatcher" trees: `tests/dispatcher/` and
  `tests/application/dispatcher/`.
- `tests/integration/` exercises only `fakeredis` — no real-service coverage,
  so the name is misleading.
- `tests/e2e/` mocks every external dependency — also misleading.
- `tests/eval/` violates PRD §8.3 (LLM-behaviour assertions belong in
  `evals/`, not `tests/`).
- `tests/_fakes/` houses 13 hand-written fake adapters that no contract test
  validates against the real implementations — fakes drift silently.
- A single `make test` target runs every layer; CI has no staging.
- `tests/conftest.py` leaks evals knowledge (clears
  `evals.runtime.config.get_eval_config` cache from the root) — a sign that
  layer boundaries have already been violated.

The user's directive: **delete everything under `tests/` and rebuild as a
proper engineered suite (unit / integration / e2e / real-service plus the
two high-value adjacent layers identified during brainstorming).**

## 2 · Goals

1. Six layers, each with an explicit contract — purpose, allowed
   dependencies, time budget, CI trigger.
2. CI staged into four phases (PR-fast / push-full / nightly / manual) with
   each phase mapping to a layer prefix, no marker gymnastics.
3. Fakes promoted from `tests/_fakes/` to `openbot/testing/`, packaged as an
   optional extra (`pip install openbot[testing]`).
4. Every port has a contract test running the **same** test code against
   the fake **and** an in-process real implementation — fake-vs-real drift
   becomes a CI red line.
5. Real-service tests consume `.env` (no docker required locally), with
   recorded GitHub VCR cassettes committed to git for deterministic
   replay.
6. Layer boundaries enforced by `import-linter`, not convention.
7. Naming / placement rules eliminate the duplication and dead-pattern
   names (`_pro`, `_verified`, `_v3`) that crept in.
8. Migration completes as a single PR — one cut, no transition period.

## 3 · Non-goals

- Test coverage as a PR-blocking threshold. Coverage is a regression
  signal in nightly, not a gate.
- Migrating `evals/` test conventions. `tests/eval/` is moved to
  `evals/tests/` as part of cleanup, but evals' internal structure is out
  of scope.
- Replacing the current pytest stack. Stays on `pytest`, `pytest-asyncio`;
  adds `respx`, `vcrpy`, `pytest-recording`, `pytest-xdist`.
- LLM-behaviour testing. Stays in `evals/` per PRD §8.3.
- Containerised local runs. `testcontainers` is not introduced; CI uses
  GitHub Actions service containers, locally `.env` drives connections.

## 4 · The Six-Layer Contract

| Layer | Tests what | Allowed deps | Forbidden deps | Budget per test | CI |
|---|---|---|---|---|---|
| **unit** | Pure functions, frozen value objects, `domain/` in full, IO-free `application/` policy | stdlib + module under test | any IO, any fake adapter, any cross-layer fixture import | < 50 ms | PR-fast |
| **contract** | `application/ports/*` protocol contracts. **Same test runs against fake and in-process real** | fakes from `openbot/testing/`, real adapters with in-process substitutes (`fakeredis` / `aiosqlite` / `respx` / `litellm.mock_response`) | network, subprocess, cross-process queues | < 200 ms | PR-fast |
| **integration** | `application/use_cases/*` with multiple ports cooperating; dispatcher; middleware chain; evaluation facade; agent runtime with fake LLM | full fake ensemble, in-memory infra | real network, real docker | < 2 s | push-full |
| **smoke** | Boot-time invariants: FastAPI app boot, worker boot, CLI loadable, `Settings()` constructible, `/ready` returns 200, alembic single head, import-linter green | full process boot | real external connections (URLs may be empty) | < 5 s for the whole layer | push-full |
| **e2e** | Full single-process pipeline: webhook → queue → worker → use case → channel adapter. **Only GitHub adapter is fake**; everything else runs the real code path | fake LLM, fake GitHub channel, in-memory `fakeredis`/`aiosqlite` | real network, real docker | < 10 s | nightly |
| **real_service** | Real Postgres / Redis (consumes `.env`), real GitHub PR via VCR cassette replay, smee webhook replay | real connection strings, cassette files | real LLM calls (those belong in `evals/`) | unbounded | manual / release |

### 4.1 Hard rules

1. **Lower-only direction**: a test in layer N may import from
   `openbot/testing/` and from any source module, but **not** from
   tests in another layer. Shared helpers always live in
   `openbot/testing/`.
2. **No fakes in `tests/`**: fakes live in `openbot/testing/fakes/`.
   Contract layer enforces fake-vs-real equivalence.
3. **Per-layer conftest, narrow**: `tests/<layer>/conftest.py` hosts only
   that layer's fixtures. The root conftest does ambient-env scrubbing
   plus the RSA key — nothing else.
4. **Path mirrors source**: `tests/unit/domain/test_events.py` tests
   `openbot/domain/events.py`. No `_v3`, `_pro`, `_verified`, `_alt`
   suffixes. One responsibility per file. Files over 400 lines must split.
5. **Tests do not import tests**: `tests/_fakes/`-style "shared test
   helpers" do not exist; helpers live in `openbot/testing/`.

## 5 · Directory Layout

```text
tests/
├── conftest.py                    # ambient-env isolation + RSA key (only)
├── pytest.ini
│
├── unit/                          # PR-fast · no IO
│   ├── conftest.py
│   ├── domain/
│   │   ├── test_events.py
│   │   ├── test_review_spec.py
│   │   └── test_decision.py
│   ├── application/
│   │   ├── test_classifier_pure.py
│   │   ├── test_dispatcher_decide.py
│   │   ├── test_router.py
│   │   └── middleware/
│   │       └── test_chain_order.py
│   ├── core/
│   │   ├── test_settings.py
│   │   └── test_metrics.py
│   └── infrastructure/
│       ├── test_github_signing.py
│       └── test_llm_sanitize.py
│
├── contract/                      # PR-fast · port contracts
│   ├── conftest.py                # parametrized fake/real factories
│   ├── test_queue_contract.py
│   ├── test_runs_repo_contract.py
│   ├── test_dedup_contract.py
│   ├── test_rate_limiter_contract.py
│   ├── test_cancellation_contract.py
│   ├── test_resource_lock_contract.py
│   ├── test_audit_log_contract.py
│   ├── test_config_loader_contract.py
│   ├── test_sandbox_contract.py
│   ├── test_sandbox_cache_contract.py
│   ├── test_channel_adapter_contract.py
│   └── test_llm_contract.py
│
├── integration/                   # push-full · multi-port collaboration
│   ├── conftest.py
│   ├── use_cases/
│   │   ├── _sut.py                # SUT factory (function, not fixture)
│   │   ├── test_triage_flow.py
│   │   ├── test_review_flow.py
│   │   ├── test_fix_flow.py
│   │   └── test_chat_flow.py
│   ├── dispatcher/
│   │   ├── test_decide_pipeline.py
│   │   ├── test_execute_handler.py
│   │   └── test_classifier_routing.py
│   ├── middleware/
│   │   ├── test_security.py
│   │   ├── test_rate_limit.py
│   │   ├── test_budget.py
│   │   ├── test_cancel.py
│   │   ├── test_preflight.py
│   │   ├── test_sanitize.py
│   │   ├── test_feature_toggle.py
│   │   └── test_audit_start.py
│   ├── persistence/
│   │   ├── test_runs_repo.py
│   │   ├── test_dedup.py
│   │   ├── test_db.py
│   │   └── test_agent_checkpointer.py
│   ├── queue/
│   │   ├── test_enqueue.py
│   │   ├── test_worker_consume.py
│   │   ├── test_worker_cancellation.py
│   │   ├── test_concurrent_supersede.py
│   │   └── test_redis_ordering.py
│   ├── agents/
│   │   ├── test_review_runtime.py
│   │   ├── test_fix_runtime.py
│   │   ├── test_chat_runtime.py
│   │   └── test_tools_schema.py
│   └── evaluation/
│       ├── test_eval_channel_adapter.py
│       └── test_runner.py
│
├── smoke/                         # push-full · < 5 s for the whole layer
│   ├── conftest.py
│   ├── test_app_boot.py
│   ├── test_worker_boot.py
│   ├── test_cli_boot.py
│   ├── test_settings_buildable.py
│   ├── test_alembic_heads.py
│   ├── test_import_linter.py
│   ├── test_contract_coverage.py  # every port has a contract test file
│   └── test_test_budget.py        # see §9.2
│
├── e2e/                           # nightly · single-process full pipeline
│   ├── conftest.py
│   ├── _assemble.py               # full-stack assembler (function)
│   ├── fixtures/
│   │   └── github_payloads/
│   ├── test_issue_to_triage.py
│   ├── test_pr_to_review.py
│   ├── test_command_to_fix.py
│   ├── test_command_to_chat.py
│   ├── test_supersede_lifecycle.py
│   └── test_error_recovery.py
│
└── real_service/                  # manual / release
    ├── conftest.py                # _env_or_skip
    ├── README.md                  # how to record cassettes
    ├── postgres/
    │   ├── conftest.py
    │   ├── test_runs_repo_real.py
    │   ├── test_dedup_real.py
    │   ├── test_audit_log_real.py
    │   ├── test_alembic_upgrade.py
    │   └── test_pg_dialect_features.py
    ├── redis/
    │   ├── conftest.py
    │   ├── test_queue_real.py
    │   ├── test_dedup_real.py
    │   ├── test_rate_limiter_real.py
    │   └── test_resource_lock_real.py
    ├── github/
    │   ├── conftest.py
    │   ├── cassettes/
    │   ├── test_pr_review_recorded.py
    │   ├── test_issue_triage_recorded.py
    │   ├── test_installation_token.py
    │   └── test_check_run_lifecycle.py
    └── smee/
        ├── conftest.py
        ├── fixtures/recorded_deliveries/
        └── test_webhook_replay.py
```

### 5.1 `pytest.ini`

```ini
[pytest]
testpaths = tests
asyncio_mode = auto
addopts = -ra --strict-markers --strict-config
markers =
    unit: layer=unit (PR)
    contract: layer=contract (PR)
    integration: layer=integration (push)
    smoke: layer=smoke (push)
    e2e: layer=e2e (nightly)
    real_service: layer=real-service (manual/release)
    requires_docker: needs local docker daemon
    requires_postgres: needs OPENBOT_DATABASE_URL
    requires_redis: needs OPENBOT_REDIS_URL
    requires_github_token: needs GITHUB_TOKEN to record VCR cassette
```

CI maps phases to layers by path — `pytest tests/<layer>` — not by marker
selection. Markers exist only for sub-skips inside `real_service/` (e.g.
`requires_docker`).

## 6 · `openbot/testing/` Package

```text
openbot/testing/
├── __init__.py            # exports Fake* and builders
├── fakes/
│   ├── queue.py           ├── runs_repo.py
│   ├── dedup.py           ├── rate_limiter.py
│   ├── resource_lock.py   ├── cancellation.py
│   ├── audit_log.py       ├── config_loader.py
│   ├── channel_adapter.py ├── sandbox.py
│   ├── sandbox_cache.py   └── llm.py
├── builders/              # build_issue_opened_event, build_run_record, …
│   ├── events.py          ├── payloads.py
│   ├── runs.py            └── decisions.py
├── inmemory/              # build_inmemory_redis / build_inmemory_db / …
│   ├── redis.py           ├── postgres.py
│   └── checkpointer.py
└── recording/
    └── github_vcr.py
```

### 6.1 Packaging

`pyproject.toml`:

```toml
[project.optional-dependencies]
testing = [
    "fakeredis>=2.26",
    "aiosqlite>=0.20",
    "vcrpy>=6.0",
    "respx>=0.21",
    "pytest>=9.0.3",
    "pytest-asyncio>=1.3.0",
    "pytest-recording>=0.13",
]
```

Production install (`pip install openbot`) does **not** include fakes.
Tests and downstream users opt in via `pip install openbot[testing]`.

### 6.2 Fake-rewrite hard rules

> **Gap to close during rebuild**: the legacy `tests/_fakes/` only ships
> 11 fakes; `openbot.application.ports.sandbox_cache` has no fake
> today. The new `openbot/testing/fakes/sandbox_cache.py` is created
> from scratch as part of this rebuild — without it, the contract
> layer cannot run the fake side of `test_sandbox_cache_contract.py`.

1. Method signatures match the port `Protocol` exactly. A module-level
   `_PROTOCOL_CHECK: Final[QueuePort] = FakeQueue()` makes type-system
   drift fail at import.
2. Observable state exposes **frozen** dataclasses (`tuple[EnqueueRecord, ...]`),
   not weak `list[dict[str, Any]]`.
3. No `if test_mode` branches; no env reads; no settings lookups. Caller
   constructs the fake with explicit kwargs.
4. Failure injection is explicit:
   `FakeRunsRepo(fail_on_create=True, failure=RetryableDBError(…))`.
5. No instance API beyond port methods plus typed observation properties
   (`events`, `task_specs`). No `.reset()`, no `.fast_forward()` — each
   test constructs a fresh fake.

### 6.3 Builders replace `sm_harness`

```python
# openbot/testing/builders/events.py
def build_issue_opened_event(
    *,
    repo: str = "owner/repo",
    issue_number: int = 1,
    sender: str = "octocat",
    body: str = "test issue body",
    delivery_id: str | None = None,
) -> UnifiedEvent: ...
```

Tests read as: `event = build_issue_opened_event(body="...")` rather
than the current 30-line state-machine harness assembly.

### 6.4 Fake self-tests are forbidden

Fakes do not get their own dedicated tests. Their correctness is
established by contract layer (§7) running the **same** suite against
fake and real. Files like `tests/infrastructure/sandboxes/test_fake.py`
do not exist in the new tree.

## 7 · Contract Layer Mechanics

### 7.1 Pattern

```python
# tests/contract/test_queue_contract.py
import pytest
from openbot.application.ports.queue import QueuePort
from openbot.testing.fakes.queue import FakeQueue
from openbot.testing.inmemory.redis import build_inmemory_redis
from openbot.testing.builders.events import build_issue_opened_event
from openbot.infrastructure.queue.redis_queue import RedisQueue


@pytest.fixture(params=["fake", "real"])
async def queue(request):
    if request.param == "fake":
        yield FakeQueue()
    else:
        async with build_inmemory_redis() as r:
            yield RedisQueue(redis=r, stream_key="test:events")


class TestQueueContract:
    async def test_enqueue_returns_stream_id(self, queue: QueuePort) -> None:
        sid = await queue.enqueue(build_issue_opened_event(),
                                  feature="triage", task_id="t1")
        assert "-" in sid and sid != ""

    async def test_enqueue_preserves_order(self, queue: QueuePort) -> None:
        sids = [await queue.enqueue(build_issue_opened_event(issue_number=i),
                                    feature="triage", task_id=f"t{i}")
                for i in range(5)]
        assert sids == sorted(sids)
```

Output: every test name appears twice, suffixed `[fake]` and `[real]`.
Drift in either direction fails CI.

### 7.2 In-process "real" implementations

| Port | Real impl in contract layer |
|---|---|
| `QueuePort` | `RedisQueue(fakeredis)` |
| `RunsRepoPort` | `SqlAlchemyRunsRepo(aiosqlite)` |
| `DedupPort` | `RedisDedup(fakeredis)` |
| `RateLimiterPort` | `RedisRateLimiter(fakeredis)` |
| `ResourceLockPort` | `RedisResourceLock(fakeredis)` |
| `CancellationPort` | `RedisCancellation(fakeredis)` |
| `AuditLogPort` | `SqlAlchemyAuditLog(aiosqlite)` |
| `ConfigLoaderPort` | `YamlConfigLoader(tmp_path)` |
| `ChannelAdapterPort` | `GitHubChannelAdapter(respx)` |
| `SandboxPort` | `DockerSandbox` (skipped without daemon); Daytona/Modal **not** in contract layer |
| `SandboxCachePort` | `InMemorySandboxCache` |
| `LLMPort` | `LiteLLMAdapter(litellm.mock_response)` |

Daytona / Modal sandboxes have no in-process substitute; their contract
correctness is covered by their own adapter unit tests plus the
`real_service` layer when credentials are available.

### 7.3 Contract hard rules

- Every port `openbot.application.ports.*` must have a corresponding
  `tests/contract/test_<name>_contract.py`. `tests/smoke/test_contract_coverage.py`
  reflects the ports module to enforce this.
- `pytest.skip` on the fake parametrization is forbidden. Skipping the
  real path is allowed only via `requires_docker` etc.
- `monkeypatch` against the implementation under test is forbidden —
  contract tests verify behaviour through the public API only.
- Contract tests do not import `openbot.application.use_cases.*`.

## 8 · Conftest Strategy

### 8.1 Root `tests/conftest.py`

Two responsibilities only:

1. Autouse ambient-env scrub — `OPENBOT_*`, LangSmith, LLM creds —
   with chdir to `tmp_path`. Applies to every test.
2. Session-scoped `rsa_private_key_pem` fixture for GitHub App tests.

The current eval-cache-clear logic is removed; it leaks an evals
dependency into pure-unit tests and belongs in `evals/tests/conftest.py`.

### 8.2 Per-layer conftests

| File | Job | Forbidden |
|---|---|---|
| `tests/unit/conftest.py` | (near empty) | any IO fixture |
| `tests/contract/conftest.py` | `inmemory_redis`, `inmemory_db_sessions`, `respx_mock` factories | use-case wiring, agent runtime |
| `tests/integration/conftest.py` | `fake_<port>` fixtures (one per port), one thing each | autouse IO, mega-fixtures |
| `tests/smoke/conftest.py` | minimal `Settings()` builder, app/worker boot helpers | full business assembly |
| `tests/e2e/conftest.py` | `e2e_stack` fixture wrapping `_assemble.py` | real network |
| `tests/real_service/conftest.py` | `_env_or_skip`, cassette path constants | business assembly |

### 8.3 SUT factories are functions, not fixtures

```python
# tests/integration/use_cases/_sut.py
@dataclass
class TriageSUT:
    use_case: TriageUseCase
    queue: QueuePort
    runs_repo: RunsRepoPort
    channel: ChannelAdapterPort
    llm: LLMPort

def make_triage_sut(*, queue, runs_repo, channel, llm, config_loader) -> TriageSUT:
    return TriageSUT(
        use_case=TriageUseCase(queue=queue, runs_repo=runs_repo,
                               channel=channel, llm=llm,
                               config_loader=config_loader),
        queue=queue, runs_repo=runs_repo, channel=channel, llm=llm,
    )
```

Tests assemble explicitly; readers see what is wired without chasing
fixture inheritance.

### 8.4 Fixture blacklist

| Anti-pattern | Reason |
|---|---|
| `autouse=True` on IO fixtures | implicit wiring leaks across tests |
| Cross-layer conftest imports | layer leak |
| Mutable shared state in fixtures | cross-test contamination |
| `monkeypatch.setattr` on internals | tests implementation, not behaviour |
| Dynamic `request.node.add_marker` | markers must be statically readable |
| Global `filterwarnings` ignore | hides real warnings |

### 8.5 Naming

| Kind | Convention |
|---|---|
| Fake instance fixture | `fake_<port>` |
| In-memory infra fixture | `inmemory_<thing>` |
| Real-service fixture | `real_<thing>` |
| SUT constructor | `make_<feature>_sut` (function, not fixture) |
| Builder | `build_<thing>` (in `openbot/testing/builders/`) |

## 9 · CI Phases & Budgets

### 9.1 Phases

| Phase | Trigger | Layers | Time budget | Failure |
|---|---|---|---|---|
| PR-fast | PR open / push | `unit` + `contract` | ≤ 90 s | block merge |
| push-full | push to `main`, `ready_for_review` | + `integration` + `smoke` | ≤ 5 min | block merge |
| nightly | schedule 07:00 UTC | + `e2e` | ≤ 15 min | notify |
| manual / release | `workflow_dispatch`, release tag | + `real_service` | ≤ 30 min | block release |

### 9.2 Budgets

| Layer | Per test | Whole layer | Files target |
|---|---|---|---|
| unit | 50 ms | 30 s | 60–80 |
| contract | 200 ms | 60 s | 12 (one per port) |
| integration | 2 s | 4 min | 30–40 |
| smoke | — | 5 s | 8–10 |
| e2e | 10 s | 12 min | 8–12 |
| real_service | unbounded | 30 min | unbounded |

`tests/smoke/test_test_budget.py` reads `.pytest_cache/durations.json`
and fails if any test exceeds its layer budget. Pre-commit runs
`pytest --durations=20` against `test-fast` so violations are caught
locally.

### 9.3 Makefile

```makefile
test-unit:           $(PYTEST) tests/unit -q
test-contract:       $(PYTEST) tests/contract -q
test-integration:    $(PYTEST) tests/integration
test-smoke:          $(PYTEST) tests/smoke
test-e2e:            $(PYTEST) tests/e2e
test-real-service:   $(PYTEST) tests/real_service

test-fast:    test-unit test-contract       # uses -n auto via pytest-xdist
test-full:    test-fast test-smoke test-integration
test-nightly: test-full test-e2e

test:         test-fast                     # default `make test`
check:        fmt-check lint lint-imports test-full
check-fast:   fmt-check lint lint-imports test-fast
```

`make test` default flips from "everything except evals" to "PR-fast
subset". Developers running `make check` get push-full equivalent
locally before pushing.

### 9.4 GitHub Actions

Two workflows: `test.yml` (PR-fast / push-full / nightly) and
`test-real-service.yml` (manual / release / scheduled). The latter
spins Postgres + Redis as service containers; local development does
**not** require docker — `.env` connection strings drive
`test-real-service` instead.

### 9.5 Parallelism

`pytest-xdist -n auto` enabled for `test-fast` only. `integration`
and `e2e` run serially because shared in-memory state across xdist
workers introduces flakiness for limited speedup.

### 9.6 Coverage

| Package | Target | Gate |
|---|---|---|
| `openbot/domain/` | 100% line + branch | nightly only |
| `openbot/application/` | ≥ 90% line | nightly only |
| `openbot/infrastructure/` | ≥ 75% line | nightly only |
| `openbot/entrypoints/` | ≥ 60% line | nightly only |
| `openbot/testing/` | not measured | n/a |

Coverage is a regression signal in nightly, never a PR gate. Layer
budgets and contract drift catch the structural regressions; coverage
catches the slow-leak regressions.

## 10 · Real-Service Layer Specifics

### 10.1 `_env_or_skip`

```python
def _env_or_skip(*keys: str) -> dict[str, str]:
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing env: {', '.join(missing)}", allow_module_level=True)
    return {k: os.environ[k] for k in keys}
```

Module-level skip ensures `pytest tests/real_service/postgres/` without
`OPENBOT_DATABASE_URL` produces one `s` per file, not 50 per test.

### 10.2 GitHub VCR

- `vcrpy>=6.0` + `pytest-recording>=0.13`.
- Default `record_mode="none"` (replay only); CLI override
  `--vcr-record=once` for first-time recording.
- Header redaction: `authorization`, `x-hub-signature`,
  `x-hub-signature-256`, `x-github-delivery`, `set-cookie`, `cookie`.
- Body redaction patterns for `"token": "…"`, `"private_key": "…"`,
  Bearer tokens.
- Cassettes live in `tests/real_service/github/cassettes/*.yaml`,
  committed to git.
- Pre-commit hook `scripts/check-cassettes.py` greps for
  `ghs_*`/`gho_*`/`ghp_*`/`AKIA*`/PEM blocks/`Bearer …` and rejects.

### 10.3 Smee replay

Smee.io is used **only** during recording. Replay-time tests post
recorded delivery JSON directly to a process-local uvicorn — no smee
involvement, no network, deterministic.

### 10.4 Real-service hard rules

- No real LLM calls. Real LLM testing belongs in `evals/`.
- Cassettes are not edited by hand. Schema drift requires re-record.
- Connection-string env vars use the existing `OPENBOT_*` prefix
  (`OPENBOT_DATABASE_URL`, `OPENBOT_REDIS_URL`). No
  `TEST_DATABASE_URL` split.
- `pytest.skip(..., allow_module_level=True)` for missing env, never
  per-test skip.

## 11 · `import-linter` Rules

Added to existing `.importlinter`:

```ini
[importlinter:contract:test-layers]
name = Test layers cannot cross
type = forbidden
source_modules = tests.unit
forbidden_modules = tests.contract, tests.integration, tests.smoke, tests.e2e, tests.real_service

[importlinter:contract:test-no-private-fakes]
name = Tests must not import legacy private fakes
type = forbidden
source_modules = tests
forbidden_modules = tests._fakes

[importlinter:contract:no-testing-in-runtime]
name = Production code must not import openbot.testing
type = forbidden
source_modules =
    openbot.domain
    openbot.application
    openbot.infrastructure
    openbot.entrypoints
    openbot.core
    openbot.dispatcher
    openbot.evaluation
forbidden_modules = openbot.testing
```

`openbot.evals` is intentionally absent from the runtime list — evals is
a documented consumer of `openbot.testing`.

## 12 · Migration: Single-PR Cut

User directive: one PR, no transition period.

### 12.1 PR contents

1. Delete the entire current `tests/` directory.
2. Create `openbot/testing/` with all 12 fakes rewritten per §6.2,
   builders, in-memory factories, recording helpers.
3. Create the new `tests/` tree per §5 with all six layers populated.
4. Move `tests/eval/` content (12 files) to `evals/tests/` as part of
   the same PR. This is technically outside the "rebuild `tests/`"
   scope but is included here because (a) it's a known PRD §8.3
   violation, (b) deleting `tests/eval/` is required to satisfy the
   acceptance checklist, and (c) splitting it into a follow-up PR
   would leave the rebuilt tree with an empty `tests/eval/` placeholder.
5. Update `pyproject.toml` (`[project.optional-dependencies] testing`,
   add `respx`, `vcrpy`, `pytest-recording`, `pytest-xdist`).
6. Update `.importlinter` with the three new contracts in §11.
7. Rewrite `Makefile` per §9.3.
8. Add `.github/workflows/test.yml` and
   `.github/workflows/test-real-service.yml`.
9. Add `scripts/check-cassettes.py` and pre-commit hook.
10. Update `CLAUDE.md` "Verification commands" section to reference the
    new layered targets.
11. Add `docs/testing/README.md` (one-page tour of the six layers).
12. Tag the pre-PR commit `pre-test-rebuild` for emergency rollback.

### 12.2 PR not in scope

- Refactoring source code in `openbot/`. Source changes appear only
  if a fake's protocol-level rewrite reveals a port signature gap.
- Recording GitHub VCR cassettes on `main`. Cassettes are recorded
  locally by the PR author, secret-scanned, and committed in the PR.
- LLM real testing setup — those tests stay in `evals/` and are not
  touched.

### 12.3 Risk & rollback

| Risk | Mitigation |
|---|---|
| Single PR is large (~300 file diff) | PR description maps each new file to the old file(s) it replaces; reviewer reads layer-by-layer |
| Real adapter behaviour gap discovered during contract writing | Contract layer is the early-detection; if a port signature is broken, fix the port (one-line change) before merging |
| Local dev flow break | `make help` advertises every new target; `docs/testing/README.md` explains the move |
| CI red on first push | Author runs `make check` + `make test-nightly` locally before opening PR |
| Need to revert | `git revert <merge-sha>` or `git reset --hard pre-test-rebuild` (tag) — single-commit reversal |

### 12.4 Acceptance checklist

- [ ] `tests/` contains only `conftest.py`, `pytest.ini`, and the six
      layer directories
- [ ] `tests/_fakes/` does not exist
- [ ] `tests/eval/` does not exist (moved to `evals/tests/`)
- [ ] `tests/state/`, `tests/state_machine/`, `tests/dispatcher/` do
      not exist
- [ ] `make test` runs in < 90 s
- [ ] `make test-full` runs in < 5 min
- [ ] `make test-nightly` runs in < 15 min
- [ ] Every port in `openbot.application.ports.*` has a contract test
- [ ] `make lint-imports` is green with all three new contracts at
      error level
- [ ] `make check` is green
- [ ] `docs/testing/README.md` is < 200 lines
- [ ] CLAUDE.md verification section updated

## 13 · Decision Log

Selected decisions made during brainstorming, captured for future
maintainers:

1. **Six layers, not four.** Adding `contract` and `smoke` over the
   user's initial four because Hexagonal architecture requires fake/real
   equivalence checking and boot-time invariants deserve their own
   class with their own time budget.
2. **Fakes in `openbot/testing/`, not `tests/_fakes/`.** Fakes are part
   of the product (consumed by `evals/` and downstream users); placing
   them in the test tree forces duplication and prevents the contract
   layer from validating equivalence.
3. **Contract tests run fake AND real** via `params=["fake","real"]`.
   This is the lever that converts "Hexagonal architecture promises
   substitutability" from a comment into a CI assertion.
4. **No docker for local real-service.** User directive — connection
   strings come from `.env`. CI uses GitHub Actions service containers
   for the same purpose.
5. **`make test` defaults to `test-fast`.** Aligns local feedback with
   PR-fast CI; full coverage is one keystroke away (`make check`).
6. **SUT factories are functions, not fixtures.** Explicit assembly
   beats fixture-magic for integration / e2e; `make_triage_sut(...)`
   reads top-down.
7. **Daytona/Modal sandboxes not in contract layer.** They have no
   in-process substitute; their correctness is verified by adapter
   unit tests plus optional `real_service` runs.
8. **Single-PR migration.** User directive — no transition period; old
   tree deleted in the same commit that introduces the new tree.

## 14 · Out-of-band notes

- This spec assumes the in-flight branch
  `refactor/evals-runtime-openbot-harness` has landed. The rebuild PR
  branches from `main` afterwards.
- `evals/tests/` becoming the new home for LLM-behaviour assertions
  formalises PRD §8.3 in code, not just in docs.
