# Phase 1a — Layer Skeleton + Sub-package Moves (Tasks 1.1 – 1.6)

> Part of [`2026-05-18-hexagonal-restructure.md`](2026-05-18-hexagonal-restructure.md). Continues into [`phase-1b`](2026-05-18-hexagonal-restructure-phase-1b-entrypoints-cleanup.md).

**Goal of Phase 1a:** create the four-layer directory skeleton and migrate every existing sub-package (`adapters/`, `persistence/`, `queue/`, `llm/`, `middleware/`, `state/`, `handlers/`, `workflows/`) plus the four top-level helpers (`events.py`, `config.py`, `obs.py`, `config_repo.py`) into their new homes — keeping back-compat shims at every old path so `make check` stays green at every commit.

6 tasks, 6 commits. Continue with Phase 1b after Task 1.6.

---

## Task 1.1: Create the directory skeleton

**Files to create (all empty `__init__.py`):**
- `openbot/domain/__init__.py`
- `openbot/application/__init__.py`
- `openbot/application/ports/__init__.py`
- `openbot/infrastructure/__init__.py`
- `openbot/core/__init__.py`
- `openbot/entrypoints/__init__.py`
- `openbot/entrypoints/api/__init__.py`
- `openbot/entrypoints/api/routes/__init__.py`
- `openbot/entrypoints/worker/__init__.py`
- `openbot/entrypoints/cli/__init__.py`

- [ ] **Step 1: Create directories and empty `__init__.py` files**

```bash
mkdir -p openbot/domain \
         openbot/application/ports \
         openbot/infrastructure \
         openbot/core \
         openbot/entrypoints/api/routes \
         openbot/entrypoints/worker \
         openbot/entrypoints/cli

for d in openbot/domain \
         openbot/application \
         openbot/application/ports \
         openbot/infrastructure \
         openbot/core \
         openbot/entrypoints \
         openbot/entrypoints/api \
         openbot/entrypoints/api/routes \
         openbot/entrypoints/worker \
         openbot/entrypoints/cli; do
  touch "$d/__init__.py"
done
```

- [ ] **Step 2: Verify package discovery**

```bash
uv run python -c "import openbot.domain, openbot.application, openbot.application.ports, openbot.infrastructure, openbot.core, openbot.entrypoints, openbot.entrypoints.api, openbot.entrypoints.api.routes, openbot.entrypoints.worker, openbot.entrypoints.cli; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add openbot/domain openbot/application openbot/infrastructure openbot/core openbot/entrypoints
git commit -m "refactor(structure): scaffold hexagonal layer directories"
```

---

## Task 1.2: Move pure-data modules into `domain/`

**Files:**
- Move: `openbot.domain.events.py` → `openbot/domain/events.py`
- Move: `openbot.domain.intents.py` → `openbot/domain/intents.py`
- Create: `openbot/domain/identifiers.py` (extract `derive_run_id` and the `task_id` SHA from `router.py`)
- Create: `openbot/domain/config_schema.py` (extract `EffectiveConfig` and friends from `config_repo.py`)

`domain/` is allowed to import only stdlib + pydantic. Do NOT move anything that touches I/O.

- [ ] **Step 1: Move `events.py` and `state/intents.py` with history preserved**

```bash
git mv openbot.domain.events.py openbot/domain/events.py
git mv openbot.domain.intents.py openbot/domain/intents.py
```

- [ ] **Step 2: Add backwards-compatible shims**

Create `openbot.domain.events.py`:

```python
"""Phase-1 shim — re-export the new domain module."""
from openbot.domain.events import *  # noqa: F401,F403
from openbot.domain.events import EventKind, UnifiedEvent  # noqa: F401
```

Create `openbot.domain.intents.py`:

```python
"""Phase-1 shim — re-export the new domain module."""
from openbot.domain.intents import *  # noqa: F401,F403
from openbot.domain.intents import Intent  # noqa: F401
```

- [ ] **Step 3: Extract `derive_run_id` to `domain/identifiers.py`**

Read `openbot.application.router.py`. Locate the `derive_run_id` function, the `_TASK_ID_HEX_LEN: Final = 32` constant, and the SHA-256 `task_id` helper. Move them verbatim to `openbot/domain/identifiers.py`. In `router.py`, replace the originals with:

```python
from openbot.domain.identifiers import derive_run_id, derive_task_id  # noqa: F401
```

- [ ] **Step 4: Extract `EffectiveConfig` to `domain/config_schema.py`**

Read `openbot.infrastructure.config_loader.py`. Locate the frozen dataclasses: `EffectiveConfig`, `BudgetConfig`, `RateLimitConfig`, `CancelConfig`, `ModelConfig`, `SecurityConfig`, `ReviewConfig`. Move all dataclasses (no I/O code) to `openbot/domain/config_schema.py`. Leave the YAML loader in `config_repo.py` for now — it moves to `infrastructure/` in Task 1.6.

In `config_repo.py` add at top:

```python
from openbot.domain.config_schema import (
    BudgetConfig,
    CancelConfig,
    EffectiveConfig,
    ModelConfig,
    RateLimitConfig,
    ReviewConfig,
    SecurityConfig,
)
```

- [ ] **Step 5: Run tests**

```bash
make test
```
Expected: 543 passed.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(domain): move pure-data modules (events, intents, identifiers, config_schema)"
```

---

## Task 1.3: Move `config.py` → `core/settings.py`; add `core/logging.py`

**Files:**
- Move: `openbot.core.settings.py` → `openbot/core/settings.py`
- Create: `openbot/core/logging.py`
- Add shim: `openbot.core.settings.py` re-exports

- [ ] **Step 1: Move `config.py`**

```bash
git mv openbot.core.settings.py openbot/core/settings.py
```

- [ ] **Step 2: Add shim**

Create `openbot.core.settings.py`:

```python
"""Phase-1 shim — re-export the new core.settings module."""
from openbot.core.settings import *  # noqa: F401,F403
from openbot.core.settings import Settings, get_settings  # noqa: F401
```

- [ ] **Step 3: Create `core/logging.py`**

```python
"""Centralized logger configuration shared by api + worker + cli."""
from __future__ import annotations

import logging
import os


def configure_root_logger() -> None:
    """Idempotent — safe to call from any entrypoint."""
    level = os.environ.get("OPENBOT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=False,
    )
```

- [ ] **Step 4: Run tests**

```bash
make test
```
Expected: 543 passed.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(core): extract settings + logging to core/"
```

---

## Task 1.4: Move I/O sub-packages to `infrastructure/`

**Files:**
- Move: `openbot.infrastructure.adapters/` → `openbot/infrastructure/adapters/`
- Move: `openbot.infrastructure.persistence/` → `openbot/infrastructure/persistence/`
- Move: `openbot.infrastructure.queue/` → `openbot/infrastructure/queue/`
- Move: `openbot.infrastructure.llm/` → `openbot/infrastructure/llm/`
- Rename: `infrastructure/llm/router.py` → `infrastructure/llm/model_router.py`
- Move: `openbot.infrastructure.observability.py` → `openbot/infrastructure/observability.py`
- Add shims at every old path

- [ ] **Step 1: Move sub-packages**

```bash
git mv openbot.infrastructure.adapters openbot/infrastructure/adapters
git mv openbot.infrastructure.persistence openbot/infrastructure/persistence
git mv openbot.infrastructure.queue openbot/infrastructure/queue
git mv openbot.infrastructure.llm openbot/infrastructure/llm
git mv openbot/infrastructure/llm/router.py openbot/infrastructure/llm/model_router.py
git mv openbot.infrastructure.observability.py openbot/infrastructure/observability.py
```

- [ ] **Step 2: Recreate the old paths as shim directories**

```bash
mkdir -p openbot.infrastructure.adapters openbot.infrastructure.persistence openbot.infrastructure.queue openbot.infrastructure.llm
```

- [ ] **Step 3: Write `openbot.infrastructure.adapters/__init__.py`**

```python
"""Phase-1 shim — re-export from infrastructure.adapters."""
from openbot.infrastructure.adapters import *  # noqa: F401,F403
from openbot.infrastructure.adapters import base, github, github_auth  # noqa: F401
```

- [ ] **Step 4: Write `openbot.infrastructure.persistence/__init__.py`**

```python
"""Phase-1 shim — re-export from infrastructure.persistence."""
from openbot.infrastructure.persistence import *  # noqa: F401,F403
from openbot.infrastructure.persistence import db, dedup, models, redis, repository  # noqa: F401
```

- [ ] **Step 5: Write `openbot.infrastructure.queue/__init__.py`**

```python
"""Phase-1 shim — re-export from infrastructure.queue."""
from openbot.infrastructure.queue import *  # noqa: F401,F403
from openbot.infrastructure.queue import enqueue, payload, runner, worker  # noqa: F401
```

- [ ] **Step 6: Write `openbot.infrastructure.llm/__init__.py` (with router alias)**

```python
"""Phase-1 shim — re-export from infrastructure.llm.

Notes: `openbot.infrastructure.llm.model_router` was renamed to `model_router` in infrastructure
to avoid colliding with `openbot.application.router`. The shim re-aliases
it so legacy `from openbot.infrastructure.llm.model_router import Feature` keeps resolving
until Task 1.11 rewrites them.
"""
from openbot.infrastructure.llm import *  # noqa: F401,F403
from openbot.infrastructure.llm import complete, sanitize  # noqa: F401
from openbot.infrastructure.llm import model_router as router  # noqa: F401
```

- [ ] **Step 7: Write `openbot.infrastructure.observability.py`**

```python
"""Phase-1 shim — re-export from infrastructure.observability."""
from openbot.infrastructure.observability import *  # noqa: F401,F403
```

- [ ] **Step 8: Confirm no internal import was left dangling**

```bash
grep -rn "openbot\.queue\.runner\|openbot\.llm\.router" openbot tests 2>/dev/null | grep -v __pycache__ | head -20
```
Every hit either lives in a shim file (fine) or is an internal caller (rewritten in Task 1.11).

- [ ] **Step 9: Run tests**

```bash
make test
```
Expected: 543 passed.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor(infrastructure): move adapters, persistence, queue, llm, observability"
```

---

## Task 1.5: Move orchestration modules to `application/`

**Files:**
- Move: `openbot.application.router.py` → `openbot/application/router.py`
- Move: `openbot.application.dispatcher.py` → `openbot/application/dispatcher.py` (note rename)
- Move: `openbot.application.middleware/` → `openbot/application/middleware/`
- Move: `openbot.application.state/` → `openbot/application/state/` (minus `intents.py`)
- Move: `openbot.application.handlers/` → `openbot/application/handlers/`
- Move: `openbot.application.workflows/` → `openbot/application/workflows/`
- Add shims at every old path

- [ ] **Step 1: Move modules**

```bash
git mv openbot.application.router.py openbot/application/router.py
git mv openbot.application.dispatcher.py openbot/application/dispatcher.py
git mv openbot.application.middleware openbot/application/middleware
git mv openbot.application.state openbot/application/state
git mv openbot.application.handlers openbot/application/handlers
git mv openbot.application.workflows openbot/application/workflows
```

- [ ] **Step 2: Recreate `openbot.application.router.py` shim**

```python
"""Phase-1 shim — re-export from application.router."""
from openbot.application.router import *  # noqa: F401,F403
from openbot.application.router import Dispatch, derive_run_id, dispatch_for, upgrade_dispatch  # noqa: F401
```

- [ ] **Step 3: Recreate `openbot.application.dispatcher.py` shim**

```python
"""Phase-1 shim — re-export from application.dispatcher."""
from openbot.application.dispatcher import *  # noqa: F401,F403
from openbot.application.dispatcher import run_dispatch  # noqa: F401
```

- [ ] **Step 4: Recreate directory shims**

```bash
mkdir -p openbot.application.middleware openbot.application.state openbot.application.handlers openbot.application.workflows
```

Write `openbot.application.middleware/__init__.py`:

```python
"""Phase-1 shim — re-export from application.middleware."""
from openbot.application.middleware import *  # noqa: F401,F403
from openbot.application.middleware import (  # noqa: F401
    audit, budget, cancel, feature_toggle, preflight,
    rate_limit, sanitize, security,
)
```

Write `openbot.application.handlers/__init__.py`:

```python
"""Phase-1 shim — re-export from application.handlers."""
from openbot.application.handlers import *  # noqa: F401,F403
from openbot.application.handlers import debug_echo  # noqa: F401
```

Write `openbot.application.workflows/__init__.py`:

```python
"""Phase-1 shim — re-export from application.workflows."""
from openbot.application.workflows import *  # noqa: F401,F403
from openbot.application.workflows import (  # noqa: F401
    _lifecycle, chat, chat_parser, fix, review, triage,
)
```

- [ ] **Step 5: Recreate `openbot.application.state/__init__.py` (special case)**

`Intent` lives in `domain.intents` after Task 1.2. Other state modules moved to `application.state`. The shim must satisfy both:

```python
"""Phase-1 shim — re-export from application.state + domain.intents."""
from openbot.application.state import *  # noqa: F401,F403
from openbot.application.state import cancellation, classifier, resource_lock, runs_repo  # noqa: F401
from openbot.domain.intents import Intent  # noqa: F401
from openbot.domain import intents  # noqa: F401  # so `openbot.domain.intents.X` resolves
```

- [ ] **Step 6: Run tests**

```bash
make test
```
Expected: 543 passed.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(application): move router, dispatcher, middleware, state, handlers, workflows"
```

---

## Task 1.6: Split `config_repo.py` into infrastructure + domain

**Files:**
- Move: `openbot.infrastructure.config_loader.py` → `openbot/infrastructure/config_loader.py`
- (Data classes already moved to `openbot/domain/config_schema.py` in Task 1.2)
- Add shim at `openbot.infrastructure.config_loader.py`

- [ ] **Step 1: Move and rename**

```bash
git mv openbot.infrastructure.config_loader.py openbot/infrastructure/config_loader.py
```

- [ ] **Step 2: Recreate shim**

Create `openbot.infrastructure.config_loader.py`:

```python
"""Phase-1 shim — re-export from infrastructure.config_loader."""
from openbot.infrastructure.config_loader import *  # noqa: F401,F403
from openbot.infrastructure.config_loader import load_for_repo  # noqa: F401
from openbot.domain.config_schema import (  # noqa: F401
    BudgetConfig, CancelConfig, EffectiveConfig, ModelConfig,
    RateLimitConfig, ReviewConfig, SecurityConfig,
)
```

- [ ] **Step 3: Run tests**

```bash
make test
```
Expected: 543 passed.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(infrastructure): rename config_repo to infrastructure.config_loader"
```

---

## End of Phase 1a

At this point:
- All eight sub-packages and four top-level helpers have moved to their new layers.
- Every old import path still resolves through a shim.
- `make test` reports 543 passed at every commit.

Continue with [Phase 1b](2026-05-18-hexagonal-restructure-phase-1b-entrypoints-cleanup.md): split `webapp.py`, move worker + CLI entrypoints, add `import-linter`, delete all shims.
