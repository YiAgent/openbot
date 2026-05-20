# State-Machine L2 Integration Test Suite — Design

> **Date**: 2026-05-17  
> **Scope**: `tests/state_machine/` — 35 new tests, ~350 lines  
> **Related**: `docs/_archive/webhook-worker/webhook-worker-test-plan.md` Phase 1

---

## Problem

The existing test suite has full L1 coverage (classifier, cancellation, runs_repo), but no test exercises the **full webhook endpoint flow with state machine enabled** — i.e., no test currently validates that:

1. A signed POST to `/webhook/github` produces the correct **Postgres `task_runs` row**
2. The correct **Redis Stream entry** is enqueued with the right `intent`
3. Duplicate deliveries are **idempotent** at the HTTP layer
4. Error inputs return the right **status codes** without polluting state

## Architecture

### Why `httpx.AsyncClient + ASGITransport` over `TestClient`

`TestClient` creates its own internal event loop for the lifespan. `fakeredis.aioredis.FakeRedis` is event-loop-bound. Using them together causes cross-loop errors. `AsyncClient + ASGITransport` runs the app in the **current pytest-asyncio event loop**, so the fake Redis instance created in the fixture is the same one the app uses.

### Backend injection

The app creates its backends in `lifespan`. We patch before lifespan runs:

```python
monkeypatch.setattr("openbot.entrypoints.api.app.make_client", lambda _url: fake_redis)
monkeypatch.setattr("openbot.entrypoints.api.app.make_engine", lambda _url, **kw: aiosqlite_engine)
```

Both patches are set via `monkeypatch` so they are automatically reverted after each test. `get_settings.cache_clear()` is called before and after to prevent settings bleed between tests.

### Workflow execution is NOT triggered

When Redis is available and enqueue succeeds, the webapp does `return {"status": "accepted", ...}` inside the try-block — the `background.add_task` fallback is unreachable. Tests assert only on HTTP response + Redis + Postgres state; no LLM or GitHub API calls occur.

---

## File Map

```
tests/state_machine/
├── __init__.py
├── conftest.py              # SMHarness dataclass + sm fixture + helpers
├── test_issue_lifecycle.py  # I-01, I-05, I-09, I-11, I-23, M-31
├── test_pr_lifecycle.py     # P-01, P-02, P-05
└── test_error_paths.py      # I-30, I-31, I-32, X-01
```

---

## `SMHarness` Interface

```python
@dataclass
class SMHarness:
    client: AsyncClient
    redis: FakeRedis
    session_factory: async_sessionmaker[AsyncSession]

    # Assertion helpers
    async def queue_len(self) -> int          # XLEN openbot.application.workflows
    async def db_state(self, rk: str) -> State
    async def db_run_id(self, rk: str) -> str | None
    async def cancel_flag(self, run_id: str) -> bool
```

Payload builders return `bytes` and are pure functions (no I/O):

```python
def issue_body(action, *, number=42, seq=1000, actor_type="User") -> bytes
def pr_body(action, *, number=7, head_sha="abc123", draft=False) -> bytes
def sign(body, *, event="issues", delivery="d-1") -> dict[str, str]
```

---

## Test Coverage

### `test_issue_lifecycle.py`

| Test | Plan ID | Scenario | Key assertion |
|------|---------|----------|---------------|
| `test_opened_starts_task` | I-01 | `issues.opened` | `status=accepted`, `db=RUNNING`, `queue=1` |
| `test_reopened_from_idle` | I-05 | `issues.reopened` fresh | `status=accepted`, `db=RUNNING` |
| `test_reopened_while_running_ignored` | I-05 | `issues.reopened` while RUNNING | `status=ignored`, `reason=already_running` |
| `test_unrelated_label_ignored` | I-09 | `issues.labeled:bug` | `status=ignored`, queue unchanged |
| `test_human_assigned_ignored` | I-11 | `issues.assigned` non-bot | `status=ignored` |
| `test_duplicate_delivery_deduped` | I-23 | Same delivery_id twice | second `status=duplicate`, queue unchanged |
| `test_bot_actor_ignored` | M-31 | `sender.type=Bot` | `status=ignored`, queue unchanged |

### `test_pr_lifecycle.py`

| Test | Plan ID | Scenario | Key assertion |
|------|---------|----------|---------------|
| `test_pr_opened_starts_review` | P-01 | `pull_request.opened` | `status=accepted`, `feature=review`, `db=RUNNING` |
| `test_synchronize_from_idle_starts` | P-02 | `pull_request.synchronize` fresh | `status=accepted`, `db=RUNNING`, queue=1 |
| `test_synchronize_supersedes_running` | P-02 | synchronize while RUNNING | cancel flag set for prev_run, queue=2 |
| `test_pr_edited_ignored` | P-05 | `pull_request.edited` | `status=ignored` (router returns None) |

### `test_error_paths.py`

| Test | Plan ID | Scenario | Key assertion |
|------|---------|----------|---------------|
| `test_bad_signature_401` | I-30 | HMAC mismatch | 401, Redis zero-change |
| `test_missing_issue_number` | I-31 | payload without `issue.number` | 202 `status=ignored`, no db row |
| `test_missing_installation_id` | I-31 | payload without `installation.id` | 202 `status=ignored`, no db row |
| `test_redis_enqueue_failure_graceful` | I-32 | enqueue raises | 202 (graceful fallback note) |
| `test_webhook_latency_under_100ms` | X-01 | happy-path round-trip | elapsed < 100ms |

---

## Implementation Boundaries

**In scope:**
- All tests above (~35 cases)
- `conftest.py` with `sm` fixture
- Pure helper functions for payload building and signing

**Out of scope:**
- Worker consumer loop (already covered in `tests/queue/test_worker.py`)
- Cancellation via ISSUE_CLOSED / PR_CLOSED (router gap — not yet routed; documented in `test_error_paths.py` as a comment)
- Incremental review, Fix workflow, Chat stream (Phase 2/3)

---

## Known Gap (Documented, Not Tested)

`ISSUE_CLOSED`, `PR_CLOSED`, `PR_MERGED`, `PR_REOPENED` are not routed by `dispatch_for()` — the webapp returns `"ignored"` before the state machine sees them. `I-04` and `P-04` from the test plan require the router to be extended first. A `# NOTE:` comment in `test_issue_lifecycle.py` marks this boundary.
