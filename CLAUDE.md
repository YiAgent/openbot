# OpenBot — Claude Code Instructions

Product spec: [`docs/prd/openbot-prd.md`](./docs/prd/openbot-prd.md) · Eval contracts: [`docs/prd/openbot-eval-prd.md`](./docs/prd/openbot-eval-prd.md) · Eval suites: [`docs/prd/openbot-eval-suites.md`](./docs/prd/openbot-eval-suites.md). Load on demand, not by default.

---

## Verification commands

After any Python change, run:

```bash
make check   # = fmt-check + lint + test
```

Other useful targets (see `make help` for the full list):

```bash
make sync    # install / sync dev deps (uv sync --dev — never pip install)
make fmt     # apply ruff formatting
make lint    # ruff check
make test    # pytest, excludes evals/
make dev     # run FastAPI with autoreload
make hooks   # install git pre-commit / pre-push hooks
```

---

## Architecture

### Main application — `openbot/`

Six-layer hexagonal architecture. Dependencies flow inward: entrypoints → application → domain; infrastructure implements ports defined by domain/application.

```
openbot/
├── core/              # Settings, logging, Sentry/Prometheus metrics
│                      #   settings.py — single source of truth for env vars (OPENBOT_* prefix)
├── domain/            # Pure domain objects — no I/O, no framework deps
│                      #   events.py, intents.py, workflows.py
│                      #   review.py, fix.py, repro.py, checkout.py, dedup.py
├── dispatcher/        # LLM-based intent classifier
│                      #   classifier.py → decide.py → direct_actions.py / incremental.py
├── application/       # Use-case orchestration + sandbox lifecycle
│                      #   dispatcher.py — routes GitHub events → agents
│                      #   sandbox_factory_deps.py / sandbox_handle.py / sandbox_policy.py
│                      #   checkout_resolver.py — resolves which commit to check out
├── infrastructure/
│   ├── adapters/      # GitHub: webhook parsing, HMAC verify, write-back (comments/labels/roles)
│   ├── agents/        # LangGraph agents — one per capability
│   │                  #   deepagents_review.py, deepagents_fix.py
│   │                  #   deepagents_repro.py, deepagents_chat.py
│   │                  #   runtime.py (BaseDeepAgentRuntime / AgentProfile)
│   │                  #   profiles.py, model_names.py, _*_tools.py, _*_schema.py
│   ├── llm/           # LLM client, model router, sanitizer
│   ├── persistence/   # Postgres (SQLAlchemy models + repos), Redis (dedup/rate-limit/resource-lock)
│   ├── queue/         # Redis-backed task queue + worker
│   └── sandboxes/     # Sandbox backends: daytona.py, fake.py (+ cache variants)
├── evaluation/        # Eval facade — called by evals/solvers/, never by prod handlers
│   ├── runner.py      # run_review / run_fix / run_chat / run_repro
│   │                  #   _open_sandbox_if_configured() — unified sandbox lifecycle for all 4 evals
│   ├── adapters.py    # Convert openbot domain outputs → Inspect AI solver format
│   ├── sandbox_file_reader.py  # Read repo files from a live sandbox
│   └── github_file_reader.py   # Read repo files via GitHub API (no sandbox)
├── entrypoints/
│   ├── api/           # FastAPI app: /webhook/github, /health
│   ├── cli/           # setup_wizard, db_init, audit
│   └── worker/        # Redis queue worker __main__
└── testing/           # Shared test infra (never imported by prod code)
    ├── fakes/         # In-memory fakes for sandbox, LLM, queue, repos, etc.
    ├── builders/      # Event / payload factory helpers
    ├── inmemory/      # In-memory Postgres, Redis, LangGraph checkpointer
    └── recording/     # GitHub VCR cassette helpers
```

**Runtime request flow:**

```
GitHub webhook
  → POST /webhook/github  (HMAC verify)
  → dispatcher (LLM classifier: decide intent)
  → application (sandbox clone → inject env)
  → infrastructure/agents/<deepagents_*.py>  (LangGraph agent loop)
  → infrastructure/adapters/github.py  (write-back: comment / label / role)
  → sandbox destroy
```

### Eval framework — `evals/`

Inspect AI drives all evals. LangSmith handles tracing, datasets, and experiment logging. Solvers call `openbot.evaluation.runner` — same prod path, unified sandbox lifecycle.

```
evals/
├── runtime/           # Inspect AI wiring + shared config
│   ├── config.py      # JudgeSettings, EvalSettings — all eval tunables (env vars)
│   └── langsmith_hook.py  # LangSmith tracing hook for Inspect AI
├── solvers/           # One solver per suite — thin wrappers over openbot.evaluation.runner
│   ├── review.py      # → runner.run_review_sample()
│   ├── fix.py         # → runner.run_fix_sample()
│   ├── chat.py        # → runner.run_chat_sample()
│   └── test_generation.py  # STUB — test-gen not yet implemented; emits unsupported=true placeholder
├── tasks/             # Inspect AI Task definitions (dataset + solver + scorers)
│   ├── review_martian.py   # PR review quality — martian_smoke_v1 dataset
│   ├── fix_swe_bench.py    # SWE-bench Verified fix
│   ├── chat_swe_qa.py      # Chat Q&A quality
│   └── test_swt_bench.py   # Test generation (SWT-bench) — the only reproduce benchmark
├── scorers/           # LLM-as-judge + overlap scorers
│   ├── review_judge.py, review_overlap.py
│   └── swe_qa_judge.py, swe_qa_pro.py
├── data/              # Dataset loaders + prediction read/write
│   ├── _base.py, _predictions.py, _writeback.py
│   └── repro.py       # ReproDataset — SWE-bench slice for repro eval
└── third_party/swt_bench/  # SWT-bench dataset integration
```

**Eval sandbox rule:** All three evals (review / fix / chat) open a sandbox via `runner._open_sandbox_if_configured()`. No solver may clone or manage sandbox directly — that is the runner's job.

### Test pyramid — `tests/`

Six layers; `make test` runs all except `real_service`.

```
tests/
├── unit/          # Pure logic, no I/O, fast (<1s total)
├── contract/      # Fake-vs-real interface contracts (port-fake dual-run)
├── integration/   # Wired with in-memory deps (Postgres/Redis/queue)
├── smoke/         # Fast end-to-end happy-path checks
├── e2e/           # Full-stack: real GitHub webhook shape, in-memory infra
└── real_service/  # Against live Postgres / Redis / GitHub (CI-optional, slow)
```

---

## Superpowers 文档归档（每 session 必检查）

当一个 slice/feature 完成提交后，立即将对应文件归档：

```bash
mv docs/superpowers/plans/<done>.md docs/_archive/superpowers/
mv docs/superpowers/specs/<done>.md  docs/_archive/superpowers/
```

Session 开始时主动检查 `docs/superpowers/plans/` 和 `docs/superpowers/specs/` 是否有已完成的文件未归档。

---

## Forbidden

- Do not commit: `.env*`, local DBs, `evals/logs/`, `.langgraph/`, `.inspect/`, `.doppler/`.
- Do not put LLM-behavior or prompt-quality assertions in `tests/` — those belong in `evals/` (PRD §8.3).
- Do not bypass pre-commit hooks (`--no-verify`).

---

## Locked boundaries (do not substitute)

- **Sandbox backend** — pluggable via `OPENBOT_SANDBOX_BACKEND` ∈ `daytona` (default) | `docker` | `fake`. Implementations in `openbot/infrastructure/sandboxes/`. Solvers and agents depend on the `SandboxBackend` protocol, never on a concrete class.
- **Observability** — LangSmith is primary (tracing, datasets, experiments, online eval). Langfuse is a fallback; do not mix them in the same request path.
- **Eval runner** — Inspect AI. LangSmith handles tracing/dataset/experiment only.
- **Channel** — GitHub only (v0.1). Do not add Slack / Discord / Linear adapter code.
- **Feature set** — `triage + review + fix + chat + repro` (v0.1). Anything outside this scope needs a new PRD slice.
- **Dependency management** — `uv sync --dev` only. Never `pip install`.

---

## Issue / domain docs

- Manage issues via the `gh` CLI; triage labels: [`docs/agents/triage-labels.md`](./docs/agents/triage-labels.md).
- Domain docs: root `CONTEXT.md` + `docs/adr/` (lazily created — silent if absent).
