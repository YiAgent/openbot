# Phase 4 — Procfile / Makefile / Docs Flip + Staging Verification (Tasks 4.1 – 4.3)

> Part of [`2026-05-18-hexagonal-restructure.md`](2026-05-18-hexagonal-restructure.md). Final phase.

**Goal of Phase 4:** flip every external string that points at the old paths — `Procfile`, `Makefile`, deployment docs, CLAUDE.md, PRD §3 — and verify the new entry points boot cleanly on staging before promoting to production.

3 tasks, 3 commits. After Task 4.3 the restructure is complete.

---

## Task 4.1: Flip `Procfile` and `Makefile` to new entry points

The web/worker process strings still point at `openbot.webapp:app` and `openbot.queue.runner`. Phase 1b's Task 1.8 left a back-compat shim at `openbot/infrastructure/queue/runner.py` until this PR — Task 4.1 deletes it after the flip.

Wait — Phase 1b Task 1.11 already deleted `openbot/infrastructure/queue/runner.py`. So the `Procfile` has been broken since Phase 1 if it still says `python -m openbot.queue.runner`. Verify before assuming.

**Files:**
- Modify: `Procfile`
- Modify: `Makefile`

- [ ] **Step 1: Inspect the current Procfile**

```bash
cat Procfile
```

You should see something like:

```
web: uvicorn openbot.webapp:app --host 0.0.0.0 --port $PORT ...
worker: python -m openbot.queue.runner
```

If either line still resolves locally (`uv run python -c "import openbot.webapp"`), the Phase 1b shim cleanup left an unrelated cache or path — investigate before flipping.

- [ ] **Step 2: Update `Procfile`**

```diff
- web: uvicorn openbot.webapp:app --host 0.0.0.0 --port $PORT --log-level info
- worker: python -m openbot.queue.runner
+ web: uvicorn openbot.entrypoints.api.app:app --host 0.0.0.0 --port $PORT --log-level info
+ worker: python -m openbot.entrypoints.worker
```

Match the existing flag set verbatim — do NOT change `--log-level`, `--workers`, `--access-log`, etc. while you're in here. The flip is path-only.

- [ ] **Step 3: Inspect the Makefile**

```bash
grep -n "openbot\." Makefile
```

Look for:

```makefile
APP ?= openbot.webapp:app
```

Or `dev:` / `run-api:` targets that hard-code the import string.

- [ ] **Step 4: Update `Makefile`**

```diff
- APP ?= openbot.webapp:app
+ APP ?= openbot.entrypoints.api.app:app
```

If `make dev` has its own string, update there too:

```diff
  dev:
- 	uv run uvicorn openbot.webapp:app --reload --port 8000
+ 	uv run uvicorn openbot.entrypoints.api.app:app --reload --port 8000
```

If a `worker` target exists, mirror it:

```diff
  worker:
- 	uv run python -m openbot.queue.runner
+ 	uv run python -m openbot.entrypoints.worker
```

- [ ] **Step 5: Verify both processes start locally**

```bash
# api — start in background, hit /health, kill.
uv run uvicorn openbot.entrypoints.api.app:app --port 8765 &
APP_PID=$!
sleep 2
curl -sf http://127.0.0.1:8765/health && echo " api OK"
kill $APP_PID

# worker — dry boot for 3s then kill. Redis connection error is OK.
OPENBOT_REDIS_URL=redis://localhost:6379 timeout 3 uv run python -m openbot.entrypoints.worker 2>&1 | head -10 || true
```

Expected: api prints `{"status":"ok","version":"..."}` then ` api OK`; worker prints a Redis connection error (or starts cleanly if Redis is up locally).

- [ ] **Step 6: Run the full gate**

```bash
make check
```
Expected: **557 passed**. `lint-imports` green.

- [ ] **Step 7: Commit**

```bash
git add Procfile Makefile
git commit -m "ops: flip Procfile and Makefile to entrypoints.* paths"
```

---

## Task 4.2: Update CLAUDE.md, PRD §3, and any other doc references

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/prd/openbot-prd.md` (and any sibling spec)
- Modify: `README.md` (if it shows the run command)
- Modify: any `.heroku/` / `.github/` / `.devcontainer/` config that pins a module path

- [ ] **Step 1: Find every stale string**

```bash
git grep -nE "openbot\.(webapp|queue\.runner|setup_wizard|config|router|dispatch|events|obs|config_repo|adapters|persistence|queue|llm|middleware|state|handlers|workflows)\b" \
    -- 'docs/**' 'README*' 'CLAUDE.md' '*.md' '.github/**' Procfile Makefile pyproject.toml
```

Each hit either belongs in this commit's rewrite or was an artifact in a generated doc. Inspect by hand.

- [ ] **Step 2: Apply the same mapping used in Phase 1b Task 1.11**

Mapping (longest-prefix first; same as `scripts/rewrite_imports.sh`):

| Old | New |
|-----|-----|
| `openbot.webapp` | `openbot.entrypoints.api.app` |
| `openbot.queue.runner` | `openbot.entrypoints.worker` |
| `openbot.setup_wizard` | `openbot.entrypoints.cli.setup_wizard` |
| `openbot.events` | `openbot.domain.events` |
| `openbot.config` | `openbot.core.settings` |
| `openbot.config_repo` | `openbot.infrastructure.config_loader` |
| `openbot.obs` | `openbot.infrastructure.observability` |
| `openbot.router` | `openbot.application.router` |
| `openbot.dispatch` | `openbot.application.dispatcher` |
| `openbot.adapters` | `openbot.infrastructure.adapters` |
| `openbot.persistence` | `openbot.infrastructure.persistence` |
| `openbot.queue` | `openbot.infrastructure.queue` |
| `openbot.llm` | `openbot.infrastructure.llm` |
| `openbot.middleware` | `openbot.application.middleware` |
| `openbot.state.intents` | `openbot.domain.intents` |
| `openbot.state` | `openbot.application.state` |
| `openbot.handlers` | `openbot.application.handlers` |
| `openbot.workflows` | `openbot.application.workflows` |

Apply by hand to each grep hit (these are docs — manual review preserves prose flow). Don't run the import-rewrite script on Markdown: it would corrupt prose that quotes the old paths intentionally (e.g. a "Why we renamed X" callout).

- [ ] **Step 3: Update the PRD §3 module table specifically**

`docs/prd/openbot-prd.md` §3 names every module by path. Open it, find §3, and rewrite each row. Example:

```diff
- | Webhook ingestion | `openbot/webapp.py` POST /webhook/github |
+ | Webhook ingestion | `openbot/entrypoints/api/routes/github_webhook.py` POST /webhook/github |
```

The PRD is the source of truth for "where things live" — every other doc that references it should align.

- [ ] **Step 4: Update CLAUDE.md**

`CLAUDE.md` has a "Locked boundaries" section. Reaffirm it but update paths:

```diff
- - v0.1 channel is GitHub only — do not write Slack / Discord / Linear adapter code in `openbot/adapters/`.
+ - v0.1 channel is GitHub only — do not write Slack / Discord / Linear adapter code in `openbot/infrastructure/adapters/`.
```

If CLAUDE.md has a section listing entry points, update there too.

- [ ] **Step 5: Verify no stale strings remain**

```bash
git grep -nE "openbot\.(webapp|queue\.runner|setup_wizard|config|router|dispatch|events|obs|config_repo|adapters|persistence|queue|llm|middleware|state|handlers|workflows)\b" \
    -- 'docs/**' 'README*' 'CLAUDE.md' '*.md' '.github/**'
```

Expected: empty output. If hits remain, inspect — most should be already-fixed in commit working tree (display lag) but anything in `git ls-files` not yet patched is a real leftover.

- [ ] **Step 6: Run the full gate**

```bash
make check
```
Expected: 557 passed (docs change doesn't move test count).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs: align CLAUDE.md, PRD §3, README to new module paths"
```

---

## Task 4.3: Staging deploy verification

The previous tasks have all been local. Task 4.3 is the bridge to production. Anything failing here gates promotion.

**Files:** none (operational verification).

- [ ] **Step 1: Deploy to staging**

```bash
# Heroku example — substitute your staging app name.
git push staging HEAD:main
```

Or whatever the project's staging deploy command is (Render, Fly, custom CI). Watch the build log for `ModuleNotFoundError` — the most common failure mode is a stale string in `Procfile` or `runtime.txt` that wasn't caught locally.

- [ ] **Step 2: Verify /health on staging**

```bash
curl -sf https://<staging-host>/health | jq .
```

Expected: `{"status": "ok", "version": "<commit-sha-prefix>"}` or similar — match what Phase 1b's `routes/health.py` returns.

- [ ] **Step 3: Verify worker is consuming**

```bash
# Heroku example: tail worker logs for the consumer-start line.
heroku logs --tail --dyno worker --app <staging-app>
```

Look for the "Starting Redis Stream consumer" (or equivalent — match what `entrypoints/worker/__main__.py` logs at startup). If the worker dyno crash-loops, capture the traceback and revert Task 4.1's `Procfile` flip while you fix.

- [ ] **Step 4: Smoke real webhook end-to-end**

If a smoke harness exists (e.g. `tests/e2e/real_e2e.py`, `tests/e2e/fire_smoke.py`), run it against staging:

```bash
SMEE_URL=<your-smee-url> \
OPENBOT_WEBHOOK_TARGET=https://<staging-host>/webhook/github \
uv run python tests/e2e/fire_smoke.py --once
```

Expected: HTTP 202 from staging, then within ~5 seconds a comment / label / status flip on the test GitHub PR (mirror of the L4 cases referenced in session notes from 2026-05-18).

- [ ] **Step 5: Confirm `OPENBOT_DEBUG_ECHO=1` end-to-end**

Per spec §11 acceptance:

```bash
heroku config:set OPENBOT_DEBUG_ECHO=1 --app <staging-app>
# fire one smoke webhook, then:
heroku logs --tail --app <staging-app> | grep debug_echo
```

Expected: the three sinks fire — GitHub comment on the test PR, a structured JSON log line tagged `debug_echo`, and a new `audit_log` row visible via the staging DB query you normally use. Unset the var after the check:

```bash
heroku config:unset OPENBOT_DEBUG_ECHO --app <staging-app>
```

- [ ] **Step 6: Promote to production**

Only after every Step 1–5 passes:

```bash
# Heroku pipeline example:
heroku pipelines:promote --app <staging-app>
# or your project's equivalent promotion command.
```

Watch prod for 15–30 minutes. If anything looks off, the previous prod release is one button away (`heroku rollback`).

- [ ] **Step 7: Mark the restructure complete**

This is not a commit — it's a session marker. Confirm against spec §11:

- [ ] `make check` (= `fmt-check + lint + lint-imports + test`) green on the new tree.
- [ ] `uvicorn openbot.entrypoints.api.app:app` boots locally and serves `/health`.
- [ ] `python -m openbot.entrypoints.worker` boots and pulls from the Redis Stream group.
- [ ] `python -m openbot.entrypoints.cli.setup_wizard` runs interactively.
- [ ] `import-linter` enforces the four-layer arrow rule (ignore list = the single documented Port→leaf-enum exception).
- [ ] 557 tests pass; no test deleted, only relocated and re-imported.
- [ ] `OPENBOT_DEBUG_ECHO=1` end-to-end webhook trace emits the three sinks (verified on staging).
- [ ] CLAUDE.md and PRD §3 module references updated to new paths.

---

## Phase 4 Acceptance

- [ ] `Procfile` and `Makefile` point at `openbot.entrypoints.*` paths.
- [ ] No doc, README, or config still references an old path (verified by `git grep`).
- [ ] Staging deploys, `/health` returns 200, worker consumes from the stream.
- [ ] Smoke webhook completes end-to-end on staging.
- [ ] `OPENBOT_DEBUG_ECHO=1` three-sink trace verified.
- [ ] Production promoted; rollback is one command away.
- [ ] `git log --oneline` shows 3 atomic commits for Tasks 4.1 – 4.3.

**Restructure complete.** All four PRs (Phase 1, 2, 3, 4) merged.
