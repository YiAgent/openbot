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

- **Sandbox = Modal**, reused per thread. Daytona / local are stub interfaces only — do not implement by default.
- **Observability = LangSmith**. Langfuse is a fallback, do not mix them.
- **Eval runner = Inspect AI**. LangSmith handles only tracing / dataset / experiment / online eval / annotation.
- v0.1 channel is GitHub only — do not write Slack / Discord / Linear adapter code.
- v0.1 feature set: `triage + review + fix + chat`. Nothing else yet.

## Implementation pace

Repo is at **v0.1 Week 1 skeleton**. `openbot/` currently contains only `webapp.py` + `config.py` + `events.py` + `adapters/`. When PRD describes a subsystem that does not exist in code, that is expected — add it as a small slice, do not implement the whole PRD at once.

## Issue / domain docs

- Manage issues via the `gh` CLI; triage labels: [`docs/agents/triage-labels.md`](./docs/agents/triage-labels.md).
- Domain docs use a single-context layout: root `CONTEXT.md` + `docs/adr/` (lazily created — silent if absent).

## Eval task workflow

When the user asks to continue eval work or says "next eval task" (or 继续下一个 eval 任务), follow the protocol in [`docs/eval/RESUME.md`](./docs/eval/RESUME.md). **One task per session.** Read [`docs/eval/STATE.md`](./docs/eval/STATE.md) first for the `Next task:` pointer, then read the matching `### <id>` section in [`docs/eval/task-list.md`](./docs/eval/task-list.md) and any `Deps:` handoffs under `docs/eval/handoffs/`. Finish by writing a handoff, updating STATE.md, and committing on branch `eval/<milestone>`.
