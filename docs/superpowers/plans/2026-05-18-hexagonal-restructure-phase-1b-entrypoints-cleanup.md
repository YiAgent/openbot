# Phase 1b — Entrypoints + Shim Cleanup (Tasks 1.7 – 1.11)

> Continues from [`phase-1a`](2026-05-18-hexagonal-restructure-phase-1a-moves.md). Read the [index](2026-05-18-hexagonal-restructure.md) for preconditions.

**Goal of Phase 1b:** split `webapp.py` into `entrypoints/api/{app,routes/*}`, move the worker and CLI process entries to `entrypoints/`, wire `import-linter` into CI, then delete every Phase-1 shim and rewrite all consumers to the new paths.

5 tasks, 5 commits. After Task 1.11 the package is fully on the new layout.

---

## Task 1.7: Split `webapp.py` into `entrypoints/api/{app,routes/*}`

The 541-line `webapp.py` decomposes into:
- `entrypoints/api/app.py` — FastAPI() + lifespan + auth construction (~150 lines)
- `entrypoints/api/routes/health.py` — GET /health (~10 lines)
- `entrypoints/api/routes/github_webhook.py` — POST /webhook/github (~250 lines)
- `entrypoints/api/deps.py` — empty placeholder

**Files:**
- Create: `openbot/entrypoints/api/app.py`
- Create: `openbot/entrypoints/api/deps.py`
- Create: `openbot/entrypoints/api/routes/health.py`
- Create: `openbot/entrypoints/api/routes/github_webhook.py`
- Replace: `openbot/webapp.py` with a shim that re-exports `app`

- [ ] **Step 1: Read `openbot/webapp.py` fully**

Identify these regions:
- **Section A**: imports + `_build_auth` helper
- **Section B**: `lifespan` async context manager + module-level globals (`_settings`, `_engine`, `_session_factory`, `_redis`, `_dedup`, `_adapter`)
- **Section C**: `FastAPI(...)` construction with `lifespan=...`
- **Section D**: route handlers — `@app.get("/health")` and `@app.post("/webhook/github")`

- [ ] **Step 2: Write `entrypoints/api/app.py`**

Copy Sections A, B, C into `openbot/entrypoints/api/app.py`. Convert the module-level globals to `app.state.*` so route modules can read them without circular imports:

```python
# Inside lifespan(app) after init:
app.state.settings = settings
app.state.engine = engine
app.state.session_factory = session_factory
app.state.redis = redis
app.state.dedup = dedup
app.state.adapter = adapter
```

Replace inline route definitions with router includes:

```python
from openbot.entrypoints.api.routes.health import router as health_router
from openbot.entrypoints.api.routes.github_webhook import router as webhook_router

# After app = FastAPI(...):
app.include_router(health_router)
app.include_router(webhook_router)
```

- [ ] **Step 3: Write `entrypoints/api/routes/health.py`**

```python
"""GET /health — liveness probe."""
from __future__ import annotations

from fastapi import APIRouter

from openbot import __version__

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
```

- [ ] **Step 4: Write `entrypoints/api/routes/github_webhook.py`**

Copy Section D's `@app.post("/webhook/github")` handler into a new `APIRouter` instance. Imports MUST use the new application/domain/infrastructure paths (no shims):

```python
"""POST /webhook/github — signed webhook ingestion."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from openbot.application.dispatcher import run_dispatch
from openbot.application.router import Dispatch, derive_run_id, dispatch_for, upgrade_dispatch
from openbot.application.state.cancellation import signal as cancellation_signal
from openbot.application.state.resource_lock import resource_lock
from openbot.application.state.runs_repo import TransitionResult, transition
from openbot.domain.intents import Intent
from openbot.infrastructure.adapters.base import SignatureError
from openbot.infrastructure.persistence import DedupOutcome
from openbot.infrastructure.queue import QueuePayload, enqueue

router = APIRouter()
_logger = logging.getLogger("openbot.api.github_webhook")


@router.post("/webhook/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(request: Request, bg: BackgroundTasks) -> dict[str, str]:
    # Copy the body of the original handler from webapp.py verbatim.
    # Replace module-level globals with request.app.state references:
    #   _settings         -> request.app.state.settings
    #   _dedup            -> request.app.state.dedup
    #   _adapter          -> request.app.state.adapter
    #   _redis            -> request.app.state.redis
    #   _session_factory  -> request.app.state.session_factory
    # Keep behavior byte-identical: 202 contract, dedup short-circuit,
    # state-machine transition, in-process vs Redis-queue branching,
    # cancellation signal. No business-logic changes in Phase 1.
    ...
```

- [ ] **Step 5: Write `entrypoints/api/deps.py` placeholder**

```python
"""FastAPI dependency factories — Phase 1 placeholder.

In Phase 2 this module will expose `get_dedup`, `get_queue`, `get_runs_repo`,
etc. Each factory returns an infrastructure implementation typed as the
application-layer Port. Until then, route handlers read collaborators
directly from `request.app.state.*`.
"""
from __future__ import annotations
```

- [ ] **Step 6: Replace `webapp.py` with a shim**

```python
"""Phase-1 shim — re-export the FastAPI app from its new home."""
from openbot.entrypoints.api.app import app  # noqa: F401
```

- [ ] **Step 7: Verify FastAPI app boots with both routes**

```bash
uv run python -c "from openbot.webapp import app; print('routes:', sorted(r.path for r in app.routes))"
```
Expected: output includes `/health` and `/webhook/github`.

- [ ] **Step 8: Run tests**

```bash
make test
```
Expected: 543 passed. `tests/test_webhook_endpoint.py` and `tests/e2e/test_webhook_events.py` are the highest-risk — pay attention to their output.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor(entrypoints): split webapp.py into entrypoints/api/{app,routes/*}"
```

---

## Task 1.8: Move worker runner to `entrypoints/worker/`

**Files:**
- Move: `openbot/infrastructure/queue/runner.py` → `openbot/entrypoints/worker/__main__.py`
- Keep `openbot.infrastructure.queue.worker` (the loop body) in place

- [ ] **Step 1: Move**

```bash
git mv openbot/infrastructure/queue/runner.py openbot/entrypoints/worker/__main__.py
```

- [ ] **Step 2: Back-compat shim at the old infra path**

The Procfile still references `openbot.queue.runner` (flip is in Phase 4 Task 4.1). Leave a shim:

Create `openbot/infrastructure/queue/runner.py`:

```python
"""Phase-1 shim — kept until Procfile flips in Phase 4."""
from openbot.entrypoints.worker.__main__ import *  # noqa: F401,F403
```

The Task 1.4 shim at `openbot/queue/__init__.py` re-exports `runner`, so `python -m openbot.queue.runner` still resolves through both layers.

- [ ] **Step 3: Dry-run boot the worker**

```bash
OPENBOT_REDIS_URL=redis://localhost:6379 timeout 2 uv run python -m openbot.entrypoints.worker 2>&1 | head -5 || true
```
Expected: no `ImportError`. A Redis connection error is acceptable if Redis isn't running locally.

- [ ] **Step 4: Run tests**

```bash
make test
```
Expected: 543 passed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(entrypoints): move worker runner to entrypoints/worker/__main__"
```

---

## Task 1.9: Move setup wizard to `entrypoints/cli/`

**Files:**
- Move: `openbot/setup_wizard.py` → `openbot/entrypoints/cli/setup_wizard.py`
- Add shim

- [ ] **Step 1: Move**

```bash
git mv openbot/setup_wizard.py openbot/entrypoints/cli/setup_wizard.py
```

- [ ] **Step 2: Shim**

Create `openbot/setup_wizard.py`:

```python
"""Phase-1 shim — re-export from entrypoints.cli.setup_wizard."""
from openbot.entrypoints.cli.setup_wizard import *  # noqa: F401,F403
```

- [ ] **Step 3: Verify CLI is loadable**

```bash
uv run python -c "import openbot.entrypoints.cli.setup_wizard as w; print(hasattr(w, 'main'))"
```
Expected: `True`.

- [ ] **Step 4: Run tests**

```bash
make test
```
Expected: 543 passed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(entrypoints): move setup wizard to entrypoints/cli"
```

---

## Task 1.10: Add `import-linter` config and CI rule

**Files:**
- Modify: `pyproject.toml` — add `import-linter` dev dep
- Create: `.importlinter`
- Modify: `Makefile`

- [ ] **Step 1: Add dev dependency**

Edit `pyproject.toml`. Find the dev dependency group (either `[dependency-groups.dev]` or `[project.optional-dependencies.dev]` — match the existing pattern). Append:

```toml
"import-linter>=2.0",
```

Then:

```bash
uv sync --dev
```

- [ ] **Step 2: Create `.importlinter`**

```ini
[importlinter]
root_package = openbot

[importlinter:contract:layers]
name = Hexagonal layers — domain ← application ← infrastructure ← entrypoints
type = layers
layers =
    openbot.entrypoints
    openbot.application : openbot.infrastructure
    openbot.domain
    openbot.core
ignore_imports =
    openbot.application.middleware.* -> openbot.infrastructure.persistence.*
    openbot.application.state.* -> openbot.infrastructure.persistence.*
    openbot.application.middleware.* -> openbot.infrastructure.llm.*
    openbot.application.workflows.* -> openbot.infrastructure.persistence.*
    openbot.application.handlers.* -> openbot.infrastructure.persistence.*
    openbot.application.handlers.* -> openbot.infrastructure.adapters.*
```

The `ignore_imports` list documents Phase-1's known cross-layer leaks. Each Phase 2 Port task deletes one or more entries.

The `openbot.application : openbot.infrastructure` syntax on the same line marks them as **independent siblings** — both allowed to depend on `openbot.domain` but not on each other in either direction. This is the formal Hexagonal arrangement.

- [ ] **Step 3: Run the contract**

```bash
uv run lint-imports
```
Expected: green (the ignore list covers current state). If the linter reports extra violations not in the ignore list, add them and re-run.

- [ ] **Step 4: Add to `make check`**

Edit `Makefile`. Locate the `check:` target:

```diff
- check: fmt-check lint test  ## Full local CI gate
+ check: fmt-check lint lint-imports test  ## Full local CI gate
```

Add the standalone target near the other lint targets:

```makefile
lint-imports:  ## Verify hexagonal layer rules
	uv run lint-imports
```

- [ ] **Step 5: Verify**

```bash
make check
```
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .importlinter Makefile
git commit -m "ci: add import-linter contract for hexagonal layers"
```

---

## Task 1.11: Delete the shim files

All callers should now resolve directly to the new paths. We delete the shims to prevent silent regressions.

**Files:**
- Delete: `openbot/events.py`, `openbot/config.py`, `openbot/router.py`, `openbot/dispatch.py`, `openbot/obs.py`, `openbot/config_repo.py`, `openbot/setup_wizard.py`, `openbot/webapp.py`
- Delete: `openbot/adapters/`, `openbot/persistence/`, `openbot/queue/`, `openbot/llm/`, `openbot/middleware/`, `openbot/state/`, `openbot/handlers/`, `openbot/workflows/` (shim dirs only)
- Delete: `openbot/infrastructure/queue/runner.py` (last remaining infra-level shim)
- Modify: every consumer across `openbot/` and `tests/` to use new paths

- [ ] **Step 1: Save the rewrite script**

Create `scripts/rewrite_imports.sh` (deleted at end of this task):

```bash
#!/usr/bin/env bash
set -euo pipefail

# Rewrite table — old → new prefix.
# Order matters: longest prefix first so partial overlaps don't corrupt.

mapping=(
  "openbot.entrypoints.cli.setup_wizard|openbot.entrypoints.cli.setup_wizard"
  "openbot.infrastructure.queue.runner|openbot.entrypoints.worker.__main__"
  "openbot.application.state.intents|openbot.domain.intents"
  "openbot.state.intents|openbot.domain.intents"
  "openbot.state|openbot.application.state"
  "openbot.handlers|openbot.application.handlers"
  "openbot.workflows|openbot.application.workflows"
  "openbot.middleware|openbot.application.middleware"
  "openbot.adapters|openbot.infrastructure.adapters"
  "openbot.persistence|openbot.infrastructure.persistence"
  "openbot.queue|openbot.infrastructure.queue"
  "openbot.llm.router|openbot.infrastructure.llm.model_router"
  "openbot.llm|openbot.infrastructure.llm"
  "openbot.obs|openbot.infrastructure.observability"
  "openbot.config_repo|openbot.infrastructure.config_loader"
  "openbot.config|openbot.core.settings"
  "openbot.events|openbot.domain.events"
  "openbot.dispatch|openbot.application.dispatcher"
  "openbot.router|openbot.application.router"
  "openbot.webapp|openbot.entrypoints.api.app"
  "openbot.setup_wizard|openbot.entrypoints.cli.setup_wizard"
)

files=$(git ls-files '*.py' '*.toml' '*.md' Procfile Makefile)

for pair in "${mapping[@]}"; do
  old="${pair%%|*}"
  new="${pair##*|}"
  echo "rewriting $old -> $new"
  for f in $files; do
    [ -f "$f" ] || continue
    case "$f" in
      scripts/rewrite_imports.sh) continue ;;
    esac
    perl -i -pe "s|\\b${old}\\b|${new}|g" "$f"
  done
done
```

- [ ] **Step 2: Run the rewrite**

```bash
chmod +x scripts/rewrite_imports.sh
./scripts/rewrite_imports.sh
```

The script rewrites shim files too — fine because we're about to delete them.

- [ ] **Step 3: Delete every shim**

```bash
rm -f openbot/events.py \
      openbot/config.py \
      openbot/router.py \
      openbot/dispatch.py \
      openbot/obs.py \
      openbot/config_repo.py \
      openbot/setup_wizard.py \
      openbot/webapp.py
rm -rf openbot/adapters \
       openbot/persistence \
       openbot/queue \
       openbot/llm \
       openbot/middleware \
       openbot/state \
       openbot/handlers \
       openbot/workflows
rm -f openbot/infrastructure/queue/runner.py
rm -f scripts/rewrite_imports.sh
```

- [ ] **Step 4: Run the full gate**

```bash
make check
```
Expected: 543 passed, `lint-imports` green.

If a test fails on `ImportError: cannot import name X from openbot.Y`: the rewrite missed a path. Find with:

```bash
git grep -E "openbot\.(events|config|router|dispatch|obs|config_repo|setup_wizard|webapp|adapters|persistence|queue|llm|middleware|state|handlers|workflows)\b" -- '*.py'
```

Patch any hit by hand, re-run `make check`.

- [ ] **Step 5: Smoke the two process entries**

```bash
# API
uv run python -c "from openbot.entrypoints.api.app import app; print('api routes:', sorted(r.path for r in app.routes))"

# Worker
uv run python -c "import openbot.entrypoints.worker.__main__ as m; print('worker module loaded:', m.__name__)"
```
Both should print without error.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(structure): remove Phase-1 shims; complete hexagonal directory move"
```

---

## Phase 1 Acceptance (covers Phase 1a + Phase 1b)

- [ ] `make check` green
- [ ] `git log --oneline` shows 11 atomic commits across Tasks 1.1 – 1.11
- [ ] `uv run python -m openbot.entrypoints.worker` and `uvicorn openbot.entrypoints.api.app:app` both boot
- [ ] No files exist at `openbot/{events,config,router,dispatch,obs,config_repo,setup_wizard,webapp}.py`
- [ ] No directories exist at `openbot/{adapters,persistence,queue,llm,middleware,state,handlers,workflows}` — those names only live under `application/` or `infrastructure/`
- [ ] `Procfile` still points to old paths — flip is in Phase 4

**Open the Phase 1 PR (11 commits across Phase 1a + 1b).** Stop here. Wait for CI green and code review before starting [Phase 2](2026-05-18-hexagonal-restructure-phase-2-ports.md).
