# State-Machine L2 Integration Test Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `tests/state_machine/` — 16 tests covering the full webhook endpoint flow (HTTP → dedup → router → state machine → Redis enqueue) using `httpx.AsyncClient + ASGITransport`, `fakeredis`, and `aiosqlite`.

**Architecture:** Patch `openbot.webapp.make_client` before the ASGI lifespan runs so the app uses a per-test `FakeRedis`. Set `OPENBOT_POSTGRES_URL=sqlite+aiosqlite:///:memory:` so lifespan creates an in-memory SQLite DB with the full schema. `AsyncClient + ASGITransport` (not `TestClient`) runs in the same event loop as `FakeRedis`.

**Tech Stack:** pytest-asyncio (auto mode), httpx + ASGITransport, fakeredis.aioredis, aiosqlite + SQLAlchemy 2.x.

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `tests/state_machine/__init__.py` | Create | Package marker |
| `tests/state_machine/_payloads.py` | Create | Payload builders + sign + constants |
| `tests/state_machine/conftest.py` | Create | `SMHarness` + `sm` async fixture |
| `tests/state_machine/test_issue_lifecycle.py` | Create | 7 tests: I-01, I-05×2, I-09, I-11, I-23, M-31 |
| `tests/state_machine/test_pr_lifecycle.py` | Create | 4 tests: P-01, P-02×2, P-05 |
| `tests/state_machine/test_error_paths.py` | Create | 5 tests: I-30, I-31×2, I-32, X-01 |

---

## Task 1: Package init + payload helpers

**Files:** `tests/state_machine/__init__.py`, `tests/state_machine/_payloads.py`

- [ ] **Step 1: Create `tests/state_machine/__init__.py`** (empty file)

- [ ] **Step 2: Create `tests/state_machine/_payloads.py`**

```python
"""Webhook payload builders for state-machine L2 tests."""
from __future__ import annotations

import hmac
import json
from hashlib import sha256
from typing import Any

_SM_SECRET: str = "sm-l2-test-secret"
_REPO: str = "acme/testrepo"
_INSTALLATION_ID: int = 99
_HUMAN: dict[str, Any] = {"login": "alice", "id": 1001, "type": "User"}
_BOT_SENDER: dict[str, Any] = {"login": "openbot[bot]", "id": 99_000, "type": "Bot"}


def sign(body: bytes, *, event: str = "issues", delivery: str = "d-1") -> dict[str, str]:
    digest = hmac.new(_SM_SECRET.encode(), body, sha256).hexdigest()
    return {
        "x-hub-signature-256": f"sha256={digest}",
        "x-github-event": event,
        "x-github-delivery": delivery,
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/sm-test",
    }


def issue_body(
    action: str,
    *,
    number: int | None = 42,
    actor_type: str = "User",
    omit_installation: bool = False,
) -> bytes:
    """Generic ``issues.*`` payload.

    ``number=None`` omits ``issue.number`` (dispatch_for returns None → ignored).
    ``omit_installation=True`` omits ``installation.id`` (same guard).
    """
    sender = _BOT_SENDER if actor_type == "Bot" else _HUMAN
    issue: dict[str, Any] = {"title": "test issue", "user": sender, "state": "open"}
    if number is not None:
        issue["number"] = number
    payload: dict[str, Any] = {
        "action": action,
        "issue": issue,
        "repository": {"full_name": _REPO, "private": False},
        "sender": sender,
    }
    if not omit_installation:
        payload["installation"] = {"id": _INSTALLATION_ID}
    return json.dumps(payload).encode()


def issue_assigned_body(*, number: int = 42, bot_assignee: bool = True) -> bytes:
    assignee: dict[str, Any] = (
        {"login": "openbot[bot]", "id": 99_000, "type": "Bot"}
        if bot_assignee
        else {"login": "bob", "id": 1002, "type": "User"}
    )
    payload: dict[str, Any] = {
        "action": "assigned",
        "issue": {"number": number, "title": "test issue", "user": _HUMAN, "state": "open"},
        "assignee": assignee,
        "repository": {"full_name": _REPO, "private": False},
        "sender": _HUMAN,
        "installation": {"id": _INSTALLATION_ID},
    }
    return json.dumps(payload).encode()


def pr_body(action: str, *, number: int = 7, head_sha: str = "abc123", draft: bool = False) -> bytes:
    payload: dict[str, Any] = {
        "action": action,
        "pull_request": {
            "number": number,
            "title": "test PR",
            "user": _HUMAN,
            "state": "open",
            "draft": draft,
            "head": {"ref": "feat/test", "sha": head_sha, "repo": {"full_name": _REPO, "fork": False}},
            "base": {"ref": "main", "sha": "0" * 40, "repo": {"full_name": _REPO, "fork": False}},
        },
        "repository": {"full_name": _REPO, "private": False},
        "sender": _HUMAN,
        "installation": {"id": _INSTALLATION_ID},
    }
    return json.dumps(payload).encode()


__all__ = ["_BOT_SENDER", "_HUMAN", "_INSTALLATION_ID", "_REPO", "_SM_SECRET",
           "issue_assigned_body", "issue_body", "pr_body", "sign"]
```

- [ ] **Step 3: Verify imports work**

```bash
cd /Users/wy/projects/openbot
python -c "from tests.state_machine._payloads import sign, issue_body, pr_body; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add tests/state_machine/__init__.py tests/state_machine/_payloads.py
git commit -m "test(state-machine): add payload builders for L2 suite"
```

---

## Task 2: SMHarness + sm fixture

**Files:** `tests/state_machine/conftest.py`

- [ ] **Step 1: Create `tests/state_machine/conftest.py`**

```python
"""State-machine L2 integration harness.

Injection: ``openbot.webapp.make_client`` is patched to return a per-test
FakeRedis before the ASGI lifespan runs. ``OPENBOT_POSTGRES_URL`` is set
to ``sqlite+aiosqlite:///:memory:`` so ``make_engine`` creates in-memory
SQLite; ``create_schema`` initialises all tables inside the lifespan.
``AsyncClient + ASGITransport`` runs the ASGI app in the current
pytest-asyncio event loop — required because FakeRedis is event-loop-bound
and TestClient creates its own internal loop.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openbot.persistence.models import State, TaskRun
from tests.state_machine._payloads import _SM_SECRET

_STREAM_NAME = "openbot:workflows"


@dataclass
class SMHarness:
    client: AsyncClient
    redis: fakeredis.aioredis.FakeRedis
    session_factory: async_sessionmaker[AsyncSession]

    async def queue_len(self) -> int:
        return await self.redis.xlen(_STREAM_NAME)

    async def _db_row(self, resource_key: str) -> TaskRun | None:
        async with self.session_factory() as session:
            return await session.get(TaskRun, resource_key)

    async def db_state(self, resource_key: str) -> State:
        row = await self._db_row(resource_key)
        return row.state if row is not None else State.IDLE

    async def db_run_id(self, resource_key: str) -> str | None:
        row = await self._db_row(resource_key)
        return row.current_run_id if row is not None else None

    async def cancel_flag(self, run_id: str) -> bool:
        return bool(await self.redis.exists(f"openbot:run_cancel:{run_id}"))


@pytest.fixture
async def sm(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SMHarness]:
    """State-machine L2 fixture: live ASGI + FakeRedis + in-memory SQLite.

    The ``_isolate_openbot_env`` autouse fixture (tests/conftest.py) strips
    all OPENBOT_* vars before each test; our monkeypatch.setenv calls layer
    on top of that strip.
    """
    from openbot.config import get_settings

    monkeypatch.setenv("OPENBOT_GITHUB_WEBHOOK_SECRET", _SM_SECRET)
    monkeypatch.setenv("OPENBOT_POSTGRES_URL", "sqlite+aiosqlite:///:memory:")
    # Any non-None URL triggers the redis_client code-path in lifespan;
    # make_client is patched below so this value is never used for a real connection.
    monkeypatch.setenv("OPENBOT_REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setenv("OPENBOT_DEBUG_ECHO_ENABLED", "false")
    get_settings.cache_clear()

    redis_fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("openbot.webapp.make_client", lambda _url: redis_fake)

    from openbot.webapp import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SMHarness(
            client=client,
            redis=redis_fake,
            session_factory=app.state.db_session_factory,
        )

    get_settings.cache_clear()
```

- [ ] **Step 2: Run fixture smoke test**

```bash
pytest tests/state_machine/ --collect-only 2>&1 | head -10
```

Expected: collection succeeds (0 errors), `conftest.py` loaded.

- [ ] **Step 3: Commit**

```bash
git add tests/state_machine/conftest.py
git commit -m "test(state-machine): add SMHarness + sm fixture"
```

---

## Task 3: Issue lifecycle tests

**Files:** `tests/state_machine/test_issue_lifecycle.py`

- [ ] **Step 1: Create the test file**

```python
"""State-machine L2: issue lifecycle (I-01, I-05×2, I-09, I-11, I-23, M-31).

NOTE (I-04): ISSUE_CLOSED is not routed by dispatch_for() — router gap.
"""
from __future__ import annotations

from openbot.persistence.models import State
from tests.state_machine._payloads import _REPO, issue_assigned_body, issue_body, sign
from tests.state_machine.conftest import SMHarness

_ISSUE_RK = f"github:{_REPO}:issue:42"


async def test_opened_starts_task(sm: SMHarness) -> None:
    """I-01: first issues.opened → DB RUNNING, one entry enqueued."""
    body = issue_body("opened", number=42)
    resp = await sm.client.post("/webhook/github", content=body, headers=sign(body, event="issues", delivery="d-01"))
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["feature"] == "triage"
    assert await sm.queue_len() == 1
    assert await sm.db_state(_ISSUE_RK) == State.RUNNING


async def test_reopened_from_idle(sm: SMHarness) -> None:
    """I-05 (fresh): issues.reopened while IDLE → RUNNING, one entry enqueued."""
    body = issue_body("reopened", number=42)
    resp = await sm.client.post("/webhook/github", content=body, headers=sign(body, event="issues", delivery="d-05-fresh"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    assert await sm.queue_len() == 1
    assert await sm.db_state(_ISSUE_RK) == State.RUNNING


async def test_reopened_while_running_ignored(sm: SMHarness) -> None:
    """I-05 (running): issues.reopened while RUNNING → IGNORE, queue unchanged."""
    body1 = issue_body("opened", number=42)
    await sm.client.post("/webhook/github", content=body1, headers=sign(body1, event="issues", delivery="d-05-open"))
    assert await sm.db_state(_ISSUE_RK) == State.RUNNING

    body2 = issue_body("reopened", number=42)
    resp = await sm.client.post("/webhook/github", content=body2, headers=sign(body2, event="issues", delivery="d-05-reopen"))
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "already_running"
    assert await sm.queue_len() == 1


async def test_unrelated_label_ignored(sm: SMHarness) -> None:
    """I-09: issues.labeled → UNKNOWN kind → router returns None → ignored."""
    body = issue_body("labeled", number=42)
    resp = await sm.client.post("/webhook/github", content=body, headers=sign(body, event="issues", delivery="d-09"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0


async def test_human_assigned_ignored(sm: SMHarness) -> None:
    """I-11: issues.assigned with human assignee → router returns None → ignored."""
    body = issue_assigned_body(number=42, bot_assignee=False)
    resp = await sm.client.post("/webhook/github", content=body, headers=sign(body, event="issues", delivery="d-11"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0


async def test_duplicate_delivery_deduped(sm: SMHarness) -> None:
    """I-23: same X-GitHub-Delivery twice → second returns status=duplicate."""
    body = issue_body("opened", number=42)
    headers = sign(body, event="issues", delivery="d-23-dup")
    resp1 = await sm.client.post("/webhook/github", content=body, headers=headers)
    assert resp1.json()["status"] == "accepted"
    assert await sm.queue_len() == 1

    resp2 = await sm.client.post("/webhook/github", content=body, headers=headers)
    assert resp2.status_code == 202
    assert resp2.json()["status"] == "duplicate"
    assert await sm.queue_len() == 1  # unchanged


async def test_bot_actor_ignored(sm: SMHarness) -> None:
    """M-31: issues.opened by Bot actor → is_from_bot → router returns None → ignored."""
    body = issue_body("opened", number=42, actor_type="Bot")
    resp = await sm.client.post("/webhook/github", content=body, headers=sign(body, event="issues", delivery="d-m31"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0
    assert await sm.db_state(_ISSUE_RK) == State.IDLE
```

- [ ] **Step 2: Run**

```bash
pytest tests/state_machine/test_issue_lifecycle.py -v
```

Expected: 7 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/state_machine/test_issue_lifecycle.py
git commit -m "test(state-machine): issue lifecycle L2 tests (I-01, I-05, I-09, I-11, I-23, M-31)"
```

---

## Task 4: PR lifecycle tests

**Files:** `tests/state_machine/test_pr_lifecycle.py`

- [ ] **Step 1: Create the test file**

```python
"""State-machine L2: PR lifecycle (P-01, P-02×2, P-05).

NOTE (P-04): PR_CLOSED / PR_MERGED not routed by dispatch_for() — router gap.
"""
from __future__ import annotations

from openbot.persistence.models import State
from tests.state_machine._payloads import _REPO, pr_body, sign
from tests.state_machine.conftest import SMHarness

_PR_RK = f"github:{_REPO}:pr:7"


async def test_pr_opened_starts_review(sm: SMHarness) -> None:
    """P-01: pull_request.opened → feature=review, DB RUNNING, one entry enqueued."""
    body = pr_body("opened", number=7, head_sha="deadbeef")
    resp = await sm.client.post("/webhook/github", content=body, headers=sign(body, event="pull_request", delivery="p-01"))
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["feature"] == "review"
    assert await sm.queue_len() == 1
    assert await sm.db_state(_PR_RK) == State.RUNNING


async def test_synchronize_from_idle_starts(sm: SMHarness) -> None:
    """P-02 (fresh): synchronize on new PR → RUNNING, one entry enqueued."""
    body = pr_body("synchronize", number=7, head_sha="sha-new")
    resp = await sm.client.post("/webhook/github", content=body, headers=sign(body, event="pull_request", delivery="p-02-fresh"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    assert await sm.queue_len() == 1
    assert await sm.db_state(_PR_RK) == State.RUNNING


async def test_synchronize_supersedes_running(sm: SMHarness) -> None:
    """P-02 (running): synchronize while RUNNING → SUPERSEDE + cancel flag for prev run."""
    body1 = pr_body("opened", number=7, head_sha="sha-first")
    await sm.client.post("/webhook/github", content=body1, headers=sign(body1, event="pull_request", delivery="p-02-open"))
    assert await sm.db_state(_PR_RK) == State.RUNNING
    run_id_1 = await sm.db_run_id(_PR_RK)
    assert run_id_1 is not None

    body2 = pr_body("synchronize", number=7, head_sha="sha-second")
    resp = await sm.client.post("/webhook/github", content=body2, headers=sign(body2, event="pull_request", delivery="p-02-sync"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"

    assert await sm.db_state(_PR_RK) == State.RUNNING
    run_id_2 = await sm.db_run_id(_PR_RK)
    assert run_id_2 is not None
    assert run_id_2 != run_id_1
    assert await sm.queue_len() == 2
    assert await sm.cancel_flag(run_id_1) is True


async def test_pr_edited_ignored(sm: SMHarness) -> None:
    """P-05: pull_request.edited → UNKNOWN kind → ignored."""
    body = pr_body("edited", number=7)
    resp = await sm.client.post("/webhook/github", content=body, headers=sign(body, event="pull_request", delivery="p-05"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0
```

- [ ] **Step 2: Run**

```bash
pytest tests/state_machine/test_pr_lifecycle.py -v
```

Expected: 4 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/state_machine/test_pr_lifecycle.py
git commit -m "test(state-machine): PR lifecycle L2 tests (P-01, P-02, P-05)"
```

---

## Task 5: Error path tests

**Files:** `tests/state_machine/test_error_paths.py`

- [ ] **Step 1: Create the test file**

```python
"""State-machine L2: error and edge-case paths (I-30, I-31×2, I-32, X-01).

I-32: when enqueue raises, the webapp falls through to BackgroundTask.
With ASGITransport, background tasks ARE executed (Starlette runs them
as part of the response lifecycle). Patch run_dispatch to a no-op so no
real GitHub API or LLM calls occur.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from tests.state_machine._payloads import _REPO, issue_body, sign
from tests.state_machine.conftest import SMHarness

_ISSUE_RK = f"github:{_REPO}:issue:42"


async def test_bad_signature_401(sm: SMHarness) -> None:
    """I-30: HMAC mismatch → 401, Redis and DB left unchanged."""
    import hmac as hmac_mod
    from hashlib import sha256 as sha256_fn

    body = issue_body("opened", number=42)
    bad_digest = hmac_mod.new(b"wrong-secret", body, sha256_fn).hexdigest()
    bad_headers = {
        "x-hub-signature-256": f"sha256={bad_digest}",
        "x-github-event": "issues",
        "x-github-delivery": "d-30",
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/test",
    }
    resp = await sm.client.post("/webhook/github", content=body, headers=bad_headers)
    assert resp.status_code == 401
    assert await sm.queue_len() == 0


async def test_missing_issue_number(sm: SMHarness) -> None:
    """I-31a: issues.opened without issue.number → dispatch_for returns None → ignored."""
    body = issue_body("opened", number=None)
    resp = await sm.client.post("/webhook/github", content=body, headers=sign(body, event="issues", delivery="d-31a"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0


async def test_missing_installation_id(sm: SMHarness) -> None:
    """I-31b: issues.opened without installation.id → dispatch_for returns None → ignored."""
    body = issue_body("opened", number=42, omit_installation=True)
    resp = await sm.client.post("/webhook/github", content=body, headers=sign(body, event="issues", delivery="d-31b"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0


async def test_redis_enqueue_failure_graceful(sm: SMHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """I-32: enqueue raises → webapp logs + falls through to BackgroundTask → still 202."""

    async def _raise_on_enqueue(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("test-enqueue-failure")

    async def _noop_dispatch(*_a: Any, **_kw: Any) -> None:
        pass

    monkeypatch.setattr("openbot.webapp.enqueue", _raise_on_enqueue)
    monkeypatch.setattr("openbot.webapp.run_dispatch", _noop_dispatch)

    body = issue_body("opened", number=42)
    resp = await sm.client.post("/webhook/github", content=body, headers=sign(body, event="issues", delivery="d-32"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    assert await sm.queue_len() == 0  # enqueue never succeeded


async def test_webhook_latency_under_100ms(sm: SMHarness) -> None:
    """X-01: happy-path issue.opened round-trip < 100 ms (in-process, no network)."""
    body = issue_body("opened", number=42)
    headers = sign(body, event="issues", delivery="d-x01")
    start = time.monotonic()
    resp = await sm.client.post("/webhook/github", content=body, headers=headers)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    assert elapsed_ms < 100, f"webhook took {elapsed_ms:.1f} ms (expected < 100 ms)"
```

- [ ] **Step 2: Run**

```bash
pytest tests/state_machine/test_error_paths.py -v
```

Expected: 5 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/state_machine/test_error_paths.py
git commit -m "test(state-machine): error path L2 tests (I-30, I-31, I-32, X-01)"
```

---

## Task 6: Full suite + CI check

- [ ] **Step 1: Run the full state-machine suite**

```bash
pytest tests/state_machine/ -v
```

Expected: 16 PASS (7 issue + 4 PR + 5 error), each < 2 s.

- [ ] **Step 2: Run `make check`**

```bash
make check
```

Expected: all existing tests still pass, no regressions.

- [ ] **Step 3: Final commit**

```bash
git add .
git commit -m "test(state-machine): L2 integration suite complete (16 tests, I-01–I-32, P-01–P-05, X-01)"
```

---

## Self-Review

**Spec coverage:** I-01 ✓ I-05×2 ✓ I-09 ✓ I-11 ✓ I-23 ✓ I-30 ✓ I-31×2 ✓ I-32 ✓ M-31 ✓ P-01 ✓ P-02×2 ✓ P-05 ✓ X-01 ✓

**Known gaps (documented in NOTE comments):** I-04 / P-04 require routing `ISSUE_CLOSED` / `PR_CLOSED` through the state machine — `dispatch_for` currently returns `None` for those events.

**No placeholders:** all test code is complete and runnable.
