# OpenBot

> Open-source, self-hosted GitHub maintenance automation for teams that want to own their bot, prompts, models, data, and cost controls.

[![status](https://img.shields.io/badge/status-pre--alpha-orange)](./docs/prd/openbot-prd.md)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](./LICENSE)
[![python](https://img.shields.io/badge/python-3.12%2B-blue)](./pyproject.toml)

OpenBot is a GitHub App backend for maintainer workflows:

- triage new issues,
- review pull requests,
- respond to `@openbot` comments,
- attempt sandboxed issue fixes,
- record cost, audit, trace, and eval data along the way.

It is designed for individual OSS maintainers and small teams who want the
control of a self-hosted bot instead of a closed hosted agent. You bring the
GitHub App, LLM keys, sandbox provider, and deployment environment.

**Status:** OpenBot is pre-alpha. Core boundaries are implemented, but the
product is not ready for unattended use on important repositories. See
[Project Status](#project-status) before installing it anywhere real.

---

## Why OpenBot

Most coding-agent products optimize for a hosted SaaS workflow. OpenBot takes a
different position:

| Principle | What it means |
|---|---|
| Self-hosted by default | You run the webhook receiver, worker, database, Redis queue, and sandbox configuration. |
| Bring your own keys | LLM credentials stay in your environment. Model routing is configurable. |
| GitHub App ownership | Each deployment uses an App controlled by the maintainer. |
| Advisory automation | Reviews comment; fixes open PRs; OpenBot never auto-merges. |
| Auditable behavior | Repo config, task state, costs, traces, and eval outputs are inspectable. |
| Eval-first development | Changes are expected to be measured through public and private eval suites. |

## Project Status

OpenBot currently has working pieces, not a finished end-to-end alpha.

| Area | Current state |
|---|---|
| Webhook ingress | FastAPI route, GitHub signature verification, event parsing, dedup, and 202 response path exist. |
| Routing | GitHub events are normalized into `triage`, `review`, `fix`, and `chat` workflow dispatches. |
| Queue contract | `TaskSpec v3` exists for Redis Stream worker execution. |
| Preflight | Sanitization, kill switch, feature toggles, cancel gates, fork-PR gate, actor role checks, rate limit, budget, and audit-start middleware are implemented. |
| Review | Structured DeepAgents review responder and GitHub PR Review submission path exist. |
| Fix | Sandboxed fix responder, branch/PR orchestration, failure comments, and no-sandbox degradation path exist. |
| Chat | Mention parsing, help/cancel handling, and basic LLM reply path exist. Repo-grounded tools are still incomplete. |
| Sandbox | `SandboxPort`, Daytona adapter, and sandbox handle plumbing exist. Worker-side sandbox factory wiring is still a closure item. |
| Evals | Public eval surfaces exist, and the eval architecture is being redesigned to call production OpenBot agents through `openbot.evaluation`. |

The current alpha closure work is tracked in:

- [Product closure spec](./docs/superpowers/specs/2026-05-22-v0-1-product-closure-design.md)
- [Eval runtime redesign spec](./docs/superpowers/specs/2026-05-22-evals-runtime-redesign.md)
- [Main PRD](./docs/prd/openbot-prd.md)

## Architecture

OpenBot is organized around a small set of product boundaries.

```text
GitHub webhook
  -> FastAPI ingress
  -> GitHubAdapter
  -> router
  -> preflight middleware
  -> TaskSpec v3
  -> Redis Stream
  -> worker
  -> dispatcher
  -> workflow handler
  -> production agent / sandbox / GitHub writeback
```

The sandbox path is owned by OpenBot, not by eval code:

```text
resolve_checkout(...)
  -> sandbox_factory()
  -> sandbox.clone(...)
  -> SandboxedHandle
  -> review / fix / chat workflow
```

Key packages:

| Path | Responsibility |
|---|---|
| `openbot/entrypoints/` | API, worker, and CLI entrypoints. |
| `openbot/application/` | Routing, dispatcher, preflight middleware, workflow use cases, checkout resolution. |
| `openbot/domain/` | Stable value objects and workflow concepts. |
| `openbot/infrastructure/` | GitHub adapter, queue, persistence, LLM routing, agents, sandboxes, observability. |
| `evals/` | Inspect AI tasks, scorer adapters, LangSmith experiment projection, prediction export. |
| `docs/prd/` | Current product and eval source-of-truth docs. |

## Quick Start

Prerequisites:

- Python 3.12+
- `uv`
- Postgres 16
- Redis 7
- a GitHub App
- an LLM provider key
- Daytona API key for sandboxed fix runs

Install dependencies:

```bash
git clone https://github.com/YiAgent/openbot.git
cd openbot
uv sync --dev
```

Start local infrastructure:

```bash
make compose-up
```

Create your environment file:

```bash
cp .env.example .env
```

Fill at least:

```text
OPENBOT_GITHUB_APP_ID
OPENBOT_GITHUB_APP_PRIVATE_KEY_PEM or OPENBOT_GITHUB_APP_PRIVATE_KEY_PATH
OPENBOT_GITHUB_WEBHOOK_SECRET
OPENBOT_POSTGRES_URL
OPENBOT_REDIS_URL
ANTHROPIC_API_KEY or another configured LLM provider key
OPENBOT_DAYTONA_API_KEY
```

Initialize the database:

```bash
make db-init
```

Run the API and worker:

```bash
make dev-server
make worker
```

Check health:

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/ready
```

For webhook forwarding during local GitHub App testing:

```bash
make dev
```

`make dev` runs the API and `smee-client` together. Set
`OPENBOT_GITHUB_WEBHOOK_PROXY_URL` in `.env` first.

## Configuration

Each target repository can include `.openbot/config.yaml`. The repo config is
intended to be reviewed like code: budget changes, feature toggles, and workflow
permissions are visible in pull requests.

Example shape:

```yaml
version: 1

features:
  triage: true
  review: true
  fix: true
  chat: true

review:
  severity_threshold: medium

fix:
  allowed_actors: [collaborator, owner]
  limits:
    max_cost_usd: 3.00
    max_wall_seconds: 2700
  pr:
    draft: false
    auto_merge: false

chat:
  rate_limit:
    per_user_per_day: 20
    per_repo_per_hour: 100
    cost_cap_per_task: 0.30

cancel:
  label: cancel-openbot
  comment_keywords: [stop, cancel, "停", "取消"]
```

See [docs/prd/openbot-config-example.yaml](./docs/prd/openbot-config-example.yaml)
for the fuller configuration reference. The example document is being aligned
with the current v0.1 alpha cutline; treat the PRD as the source of truth when
there is drift.

## Safety Model

OpenBot is intentionally conservative.

| Control | Behavior |
|---|---|
| No auto-merge | Fix workflows may open PRs, but humans merge them. |
| Fork PR gate | Forked PRs are not executed by default. |
| Budget gates | Per-task and monthly budget checks happen before expensive work. |
| Rate limits | Chat and workflow triggers are rate-limited by repo/user context. |
| Cancellation | Maintainers can cancel with labels, comments, or deployment kill switch. |
| Audit trail | Workflow lifecycle, cost, and failure state are persisted. |
| Sandbox boundary | Code-changing workflows are expected to run inside `SandboxPort`. |

Pre-alpha caveat: not every planned guard is enforced at every final execution
point yet. The alpha readiness criteria are documented in
[docs/prd/openbot-prd.md](./docs/prd/openbot-prd.md).

## Evals

OpenBot uses evals as product verification, not as a separate demo agent.

The target architecture is:

```text
Inspect task
  -> thin eval solver
  -> openbot.evaluation facade
  -> production OpenBot harness / agent / sandbox
  -> scorer
  -> LangSmith experiment + feedback
```

The eval system keeps four current surfaces:

| Suite | Measures | Status |
|---|---|---|
| `review_martian` | Review finding precision / recall / F1 on Martian CRB. | Kept; moving to production OpenBot facade. |
| `fix_swe_bench` | Patch prediction export for SWE-bench Verified. | Kept; official grading remains offline. |
| `chat_swe_qa` | Repo QA answer quality with SWE-QA-Pro judge. | Kept; must reflect real product chat capability. |
| `test_swt_bench` | Test-generation diagnostic on SWT-Bench. | Surface kept; returns `unsupported=true` until product capability exists. |

Eval documentation:

- [Eval PRD](./docs/prd/openbot-eval-prd.md)
- [Eval suite definitions](./docs/prd/openbot-eval-suites.md)
- [Eval runtime redesign](./docs/superpowers/specs/2026-05-22-evals-runtime-redesign.md)

Common commands:

```bash
make -C evals help
make -C evals test
make -C evals smoke-review
```

The current `evals/Makefile` still contains legacy target names while the
runtime redesign is in progress.

## Development

Use `make help` to see the local development commands.

```bash
make sync          # uv sync --dev
make fmt           # ruff format
make lint          # ruff check
make lint-imports  # import-linter boundary checks
make test          # pytest, excluding evals
make check         # fmt-check + lint + import rules + tests
make db-init       # create database schema
make dev-server    # run FastAPI with reload
make worker        # run Redis Stream worker
```

Recommended before opening a PR:

```bash
make check
make -C evals test
```

## Deployment

The current deployment target is Heroku:

- `web` dyno: FastAPI webhook receiver
- `worker` dyno: Redis Stream worker
- Postgres: external managed Postgres
- Redis: managed Redis
- Sandbox: Daytona
- Secrets: Doppler-first

See [docs/deploy/heroku.md](./docs/deploy/heroku.md) for the operational
runbook.

## Documentation

| Document | Purpose |
|---|---|
| [Main PRD](./docs/prd/openbot-prd.md) | Product vision, current implementation state, alpha readiness, roadmap. |
| [Eval PRD](./docs/prd/openbot-eval-prd.md) | Eval architecture and product measurement rules. |
| [Eval suites](./docs/prd/openbot-eval-suites.md) | Suite names, datasets, metrics, and gates. |
| [Product closure spec](./docs/superpowers/specs/2026-05-22-v0-1-product-closure-design.md) | Concrete gaps before v0.1 alpha is runnable. |
| [Eval runtime redesign](./docs/superpowers/specs/2026-05-22-evals-runtime-redesign.md) | Planned eval cleanup and naming rules. |
| [Heroku runbook](./docs/deploy/heroku.md) | Deployment, secrets, monitoring, and operations. |

## Contributing

OpenBot is early. The most useful contributions right now are narrow:

- fix a documented v0.1 alpha gap,
- add or repair tests around an existing boundary,
- align stale docs with code,
- improve eval reliability without adding eval-only agents or sandboxes.

Before sending a larger change, open an issue or PR draft with the intended
scope. The repository uses conventional commits and import-boundary checks.

## License

Apache-2.0. See [LICENSE](./LICENSE).

<!-- E2E review-v2 test -->
