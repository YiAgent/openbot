# OpenBot — Claude Code Instructions

Product spec lives in [`docs/prd/openbot-prd.md`](./docs/prd/openbot-prd.md). Load it on demand, not by default.

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

## Forbidden

- Do not commit: `.env*`, local DBs, `evals/logs/`, `.langgraph/`, `.inspect/`, `.doppler/`.
- Do not put LLM-behavior or prompt-quality assertions in `tests/` — those belong in `evals/` (PRD §8.3).
- Do not bypass pre-commit hooks (`--no-verify`).

## Locked boundaries (do not substitute)

- **Sandbox = pluggable via `evals.sandboxes.factory`** (`OPENBOT_SANDBOX_BACKEND` ∈ `daytona` | `modal` | `docker`, default `daytona`). All three are real implementations behind the same `SandboxBackend` protocol; solvers depend on the protocol, never on a concrete class.
- **Observability = LangSmith**. Langfuse is a fallback, do not mix them.
- **Eval runner = Inspect AI**. LangSmith handles only tracing / dataset / experiment / online eval / annotation.
- v0.1 channel is GitHub only — do not write Slack / Discord / Linear adapter code.
- v0.1 feature set: `triage + review + fix + chat`. Nothing else yet.

## Implementation pace

Repo is at **v0.1 Week 1 skeleton**. `openbot/` currently contains only `webapp.py` + `config.py` + `events.py` + `adapters/`. When PRD describes a subsystem that does not exist in code, that is expected — add it as a small slice, do not implement the whole PRD at once.

## Issue / domain docs

- Manage issues via the `gh` CLI; triage labels: [`docs/agents/triage-labels.md`](./docs/agents/triage-labels.md).
- Domain docs use a single-context layout: root `CONTEXT.md` + `docs/adr/` (lazily created — silent if absent).

## Eval workflow

Eval scope and per-suite contracts live in [`docs/prd/openbot-eval-prd.md`](./docs/prd/openbot-eval-prd.md) (commitments / gates / SLOs) and [`docs/prd/openbot-eval-suites.md`](./docs/prd/openbot-eval-suites.md) (per-cell suite details). Eval code is under `evals/` per PRD §6.1; datasets live in LangSmith (allowlist routing in `evals/common/langsmith.py`).
