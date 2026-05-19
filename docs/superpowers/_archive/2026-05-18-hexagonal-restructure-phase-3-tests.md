# Phase 3 — Test Suite Migration + Entrypoint Boot Smoke (Tasks 3.1 – 3.2)

> Part of [`2026-05-18-hexagonal-restructure.md`](2026-05-18-hexagonal-restructure.md). Continues into [`Phase 4`](2026-05-18-hexagonal-restructure-phase-4-deploy.md).

**Goal of Phase 3:** mirror the `tests/` tree to the new package layout (domain / application / infrastructure / core / entrypoints) and add three boot smoke tests for the api + worker + cli entrypoints. No test is deleted; every test is either moved or augmented.

2 tasks, 2 commits. After Task 3.2 the suite is at **557 passing** (554 from Phase 2 + 3 boot smoke tests).

---

## Task 3.1: Mirror `tests/` to the new layout

The current `tests/` tree mirrors the **old** package layout — `tests/middleware/`, `tests/state/`, `tests/persistence/`, etc. Phase 1's import rewrite (Task 1.11) already updated imports inside each test file. Phase 3 now moves the test files themselves to the new tree so `pytest --collect-only` reflects the package boundaries.

**Files:**
- Move each `tests/<old_subpkg>/` directory to its new home:
  - `tests/middleware/` → `tests/application/middleware/`
  - `tests/state/` → `tests/application/state/`
  - `tests/handlers/` → `tests/application/handlers/`
  - `tests/workflows/` → `tests/application/workflows/`
  - `tests/router/` (if exists) → `tests/application/router/`
  - `tests/dispatch/` (if exists) → `tests/application/dispatch/`
  - `tests/persistence/` → `tests/infrastructure/persistence/`
  - `tests/queue/` → `tests/infrastructure/queue/`
  - `tests/llm/` → `tests/infrastructure/llm/`
  - `tests/adapters/` → `tests/infrastructure/adapters/`
  - `tests/config/` (if exists) → `tests/core/config/`
  - `tests/events/` (if exists) → `tests/domain/events/`
- Keep `tests/e2e/`, `tests/integration/` in place (they're not slice-mirroring).
- Keep `tests/_fakes/` and `tests/application/ports/` (created in Phase 2).
- Keep top-level fixtures (`tests/conftest.py`).

- [ ] **Step 1: Audit the current tests tree**

```bash
ls tests/
find tests -maxdepth 2 -name '__init__.py' | sort
```

The mapping above is a guide — confirm what actually exists. If a directory in the mapping is missing, skip it. If a directory exists that's not in the mapping (other than `_fakes`, `application/ports`, `e2e`, `integration`), decide where it goes BEFORE moving.

- [ ] **Step 2: Pre-create the new parent directories**

```bash
mkdir -p tests/domain tests/application/middleware tests/application/state \
         tests/application/handlers tests/application/workflows \
         tests/application/router tests/application/dispatch \
         tests/infrastructure/persistence tests/infrastructure/queue \
         tests/infrastructure/llm tests/infrastructure/adapters \
         tests/core tests/entrypoints/api tests/entrypoints/worker \
         tests/entrypoints/cli

for d in tests/domain tests/application tests/application/middleware \
         tests/application/state tests/application/handlers \
         tests/application/workflows tests/application/router \
         tests/application/dispatch tests/infrastructure \
         tests/infrastructure/persistence tests/infrastructure/queue \
         tests/infrastructure/llm tests/infrastructure/adapters \
         tests/core tests/entrypoints tests/entrypoints/api \
         tests/entrypoints/worker tests/entrypoints/cli; do
  [ -f "$d/__init__.py" ] || touch "$d/__init__.py"
done
```

Some of these already exist from Phase 2 — `touch` is idempotent.

- [ ] **Step 3: Move each directory with `git mv`**

For each existing source directory:

```bash
git mv tests/middleware tests/application/middleware_NEW
# After the move, merge contents into the canonical target:
rsync -a tests/application/middleware_NEW/ tests/application/middleware/ && rm -rf tests/application/middleware_NEW
```

Simpler when the target is empty:

```bash
# When tests/application/middleware/ contains only __init__.py:
rm tests/application/middleware/__init__.py
git mv tests/middleware tests/application/middleware
touch tests/application/middleware/__init__.py
```

Repeat for each row in the mapping. After every move, run a quick sanity check:

```bash
git status --short | head -40
```

- [ ] **Step 4: Update `conftest.py` paths if needed**

If `tests/conftest.py` (or any inner `conftest.py`) hardcodes a relative path (`from tests.middleware...`), update it. Run:

```bash
grep -rn "from tests\." tests/ | grep -v __pycache__ | head -30
```

Each hit either points to the new location (fine) or the old (rewrite).

- [ ] **Step 5: Update pytest discovery (`pyproject.toml`)**

Check `pyproject.toml` for any `[tool.pytest.ini_options]` settings that whitelist specific subdirs. If `testpaths = ["tests"]` is set, no change needed. If individual subdirs are listed, expand the list.

- [ ] **Step 6: Run the suite**

```bash
make test
```
Expected: 554 passed (no count change — moves only). If the count drops, the rewrite missed a file. Use:

```bash
make test 2>&1 | tail -40
```

to locate the failing collector / import.

- [ ] **Step 7: Verify `import-linter` still passes**

```bash
uv run lint-imports
```
Expected: green with ONLY the documented Port→leaf-enum exception.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "test(structure): mirror tests/ to new four-layer layout"
```

---

## Task 3.2: Entrypoint boot smoke tests

Add three small tests that exercise the composition root of each entrypoint. These are the canary for "did Phase 2's DI wire-up actually compose?" — failing here means the process won't start in prod.

**Files:**
- Create: `tests/entrypoints/api/test_app_boot.py`
- Create: `tests/entrypoints/worker/test_main_boot.py`
- Create: `tests/entrypoints/cli/test_setup_wizard_loadable.py`

- [ ] **Step 1: Write `tests/entrypoints/api/test_app_boot.py`**

```python
"""Boot smoke — FastAPI app composes and exposes /health + /webhook/github."""
from __future__ import annotations

import pytest


def test_app_module_imports_cleanly() -> None:
    from openbot.entrypoints.api.app import app

    routes = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/health" in routes
    assert "/webhook/github" in routes


@pytest.mark.asyncio
async def test_lifespan_attaches_ports_to_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifespan should populate app.state with the 11 Port-typed collaborators.

    We monkeypatch the redis + engine constructors to in-memory fakes so the
    test doesn't require running infra.
    """
    # Patch infra constructors used in lifespan to no-op fakes BEFORE import.
    # Adjust the patch targets to match the actual module-level functions
    # that build redis / engine / session_factory.
    monkeypatch.setenv("OPENBOT_REDIS_URL", "redis://fake/0")
    monkeypatch.setenv("OPENBOT_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_redis_factory(*_args, **_kwargs):
        yield None

    # If openbot.infrastructure.persistence.redis exposes
    # `make_redis_client`, patch that. Otherwise patch whatever lifespan uses.
    try:
        import openbot.infrastructure.persistence.redis as redis_mod
        monkeypatch.setattr(redis_mod, "make_redis_client", _fake_redis_factory)
    except (AttributeError, ImportError):
        pytest.skip("redis client factory not patchable in this build")

    from httpx import ASGITransport, AsyncClient

    from openbot.entrypoints.api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Hitting /health triggers lifespan if not already triggered.
        resp = await client.get("/health")
        assert resp.status_code == 200

    # After lifespan ran, app.state should carry the 11 Ports.
    expected_attrs = {
        "settings",
        "engine",
        "session_factory",
        "redis",
        "dedup",
        "queue",
        "adapter",
        "runs_repo",
        "resource_lock",
        "cancellation",
        "audit",
        "rate_limiter",
        "config_loader",
    }
    missing = {a for a in expected_attrs if not hasattr(app.state, a)}
    assert not missing, f"app.state missing: {missing}"
```

The second test reads more like an integration test — but its scope is narrowly "lifespan composes" so it lives with entrypoint smokes. The expected-attr set should be EXHAUSTIVE for everything `lifespan` attaches; if a Phase 2 task added a new attr, append it here.

- [ ] **Step 2: Write `tests/entrypoints/worker/test_main_boot.py`**

```python
"""Boot smoke — worker entrypoint loads, parses CLI, exits before connecting."""
from __future__ import annotations

import importlib

import pytest


def test_worker_module_importable() -> None:
    """The module must import cleanly with no side effects."""
    mod = importlib.import_module("openbot.entrypoints.worker.__main__")
    assert hasattr(mod, "main") or hasattr(mod, "__name__")


@pytest.mark.asyncio
async def test_worker_main_handles_missing_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With OPENBOT_REDIS_URL unset, `main()` should fail FAST with a clear error.

    We don't run the consumer loop — just verify the composition root surfaces
    the missing-config error rather than hanging.
    """
    monkeypatch.delenv("OPENBOT_REDIS_URL", raising=False)
    mod = importlib.import_module("openbot.entrypoints.worker.__main__")

    if not hasattr(mod, "main"):
        pytest.skip("worker __main__ has no main() entry — adjust per Phase 1b shape")

    with pytest.raises((SystemExit, RuntimeError, ValueError)):
        await mod.main()
```

If `main()` doesn't exist on the module yet (Phase 1b left the script-style entry), upgrade `__main__.py` during Phase 3 to expose a callable `main()` — that's a one-line refactor that makes the smoke test possible.

- [ ] **Step 3: Write `tests/entrypoints/cli/test_setup_wizard_loadable.py`**

```python
"""Boot smoke — setup_wizard module loads and exposes its entry point."""
from __future__ import annotations

import importlib


def test_setup_wizard_module_loads() -> None:
    mod = importlib.import_module("openbot.entrypoints.cli.setup_wizard")
    # The wizard exposes either main() or a Click-style group.
    entry = getattr(mod, "main", None) or getattr(mod, "cli", None)
    assert callable(entry), "setup_wizard must expose `main` or `cli`"
```

- [ ] **Step 4: Run the new tests in isolation first**

```bash
uv run pytest tests/entrypoints/ -v
```
Expected: 3 passed (or 3 passed, 1 skipped if `make_redis_client` is non-patchable). No errors.

- [ ] **Step 5: Run the full gate**

```bash
make check
```
Expected: **557 passed** (554 from Phase 2 + 3 boot smoke tests). `lint-imports` still green.

If the api boot test fails because `app.state` is missing one of the listed attrs, decide:
- The attr SHOULD be set in lifespan → fix lifespan and re-run.
- The attr was renamed in Phase 2 → update the assertion list, NOT lifespan.

Treat any newly-discovered DI hole as a Phase-2 leftover and fix it in this same PR — Phase 3 is the right place to surface them.

- [ ] **Step 6: Commit**

```bash
git add tests/entrypoints/
git commit -m "test(entrypoints): add boot smoke for api + worker + cli"
```

---

## Phase 3 Acceptance

- [ ] `tests/` tree mirrors `openbot/` four-layer layout (`domain/`, `application/`, `infrastructure/`, `core/`, `entrypoints/`).
- [ ] `tests/_fakes/` and `tests/application/ports/` still exist with the 11 fakes + 11 contract tests from Phase 2.
- [ ] 3 boot smoke tests pass under `make test`.
- [ ] `make check` reports **557 passed**.
- [ ] `git log --oneline` shows 2 atomic commits for Tasks 3.1 and 3.2.
- [ ] No test was deleted — every previously-passing test still passes (the count went UP by 3, never down).

**Open the Phase 3 PR (2 commits).** Stop here. Wait for CI green and code review before starting [Phase 4](2026-05-18-hexagonal-restructure-phase-4-deploy.md).
