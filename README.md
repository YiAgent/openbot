# OpenBot

> **Self-hosted, customizable, open-source GitHub maintainer bot — with a public eval system you can run yourself.**

[![status](https://img.shields.io/badge/status-pre--alpha-orange)](./docs/prd/openbot-prd.md)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](./LICENSE)

OpenBot turns a single GitHub App you control into a maintainer co-pilot that takes an issue from arrival to PR. It **triages**, **reproduces**, **localizes**, **fixes**, and **reviews** — automatically on webhook, or on `@openbot` mention. Every step runs against your own API keys, your own config, your own sandbox; nothing leaves your infrastructure.

OpenBot is also a public testbed for AI software engineering: every release ships through a complete eval system that combines third-party benchmarks, your own production runs (dogfooding), and live external datasets — so you can see exactly how good the bot is on each task before trusting it on yours.

> **Status: pre-alpha.** Spec is locked ([PRD](./docs/prd/openbot-prd.md)); v0.1 skeleton is in flight. Not yet runnable end-to-end.

---

## The pipeline

OpenBot models GitHub maintenance as a five-stage pipeline, with `@mention` chat orthogonal:

```
  issue arrives ──▶ 1. triage ──▶ 2. reproduce ──▶ 3. localize ──▶ 4. fix ──▶ 5. review (PR by anyone)
                       │              │                │             │           ▲
                       └──── label, priority           │             │           │
                                       └── sandbox repro + evidence  │           │
                                                       └── candidate files       │
                                                                     └── PR opened (never auto-merge)
                                                                                 │
                                  @openbot mention ─────── chat ──────────────── ┘
```

Each stage has its own budget, kill switch, and eval suite. Any stage can be disabled per repo via `.openbot/config.yaml`.

| Stage | Trigger | Default budget | Default action |
|---|---|---|---|
| Triage | `issue.opened` | $0.20 / issue | Auto-label + priority |
| Reproduce | after triage | included | Sandbox attempt + post evidence comment |
| Localize | issue assigned to bot | $0.50 / issue | Comment with candidate files / functions |
| Fix | issue assigned to bot | $3.00 / task | Open PR (never auto-merge) |
| Review | `pull_request.opened` / `synchronize` | $0.50 / PR | Inline severity-filtered comments (never blocks merge) |
| Chat | comment `@openbot ...` | $0.30 / call | Read-only tool whitelist |

## How work reaches the bot

OpenBot uses a `ChannelAdapter` abstraction designed for multiple sources from day one — your bot doesn't care where the work came from.

| Channel | Status | Trigger |
|---|---|---|
| GitHub webhooks | v0.1 (now) | App-installed events |
| Linear | v0.2 | Issue created / commented |
| Slack | v0.3 | Slash command / mention |
| Web dashboard | v0.3+ | Manual run / scheduled task |

Any new channel implements one ABC. Same pipeline, different entry points.

## Quick start

```bash
# 1. Create your own GitHub App (one-time, ~10 min)
#    Permissions: contents:rw, issues:rw, pull_requests:rw, metadata:r
#    Subscribe to: issues, pull_request, issue_comment

# 2. Clone and configure
git clone https://github.com/<you>/openbot && cd openbot
cp .env.example .env             # or use Doppler — see Development setup

# 3. Run
docker compose up
# Webhook URL is now live at http://<your-host>/webhook
```

Default LLM is Anthropic Claude via [LiteLLM](https://github.com/BerriAI/litellm); set `OPENAI_API_KEY` or `GOOGLE_API_KEY` to route to any of 100+ providers.

## Customize per repo

Each repo gets a `.openbot/config.yaml` that's PR-able and auditable — your bot's behavior is part of your codebase:

```yaml
enabled_stages: [triage, reproduce, localize, fix, review, chat]

triage:
  labels: [bug, feature, docs, question, duplicate, needs-info]
  priority: true

fix:
  max_cost_usd: 3.00
  allowed_paths: ['src/**', '!src/security/**']   # never touch security/
  require_assignee: 'openbot[bot]'

chat:
  allowed_tools: [read_file, list_files, git_log]  # opt-in tool whitelist

budgets:
  per_task_hard_cap_usd: 5
  monthly_repo_soft_cap_usd: 50
  instance_monthly_kill_usd: 500
```

A change to this file is itself a PR.

## Safety and cost controls

Three independent layers, all locked in v0.1:

- **Three-layer cost cap** — per-task hard limit · per-repo monthly soft cap · per-instance monthly kill switch
- **Three-layer rate limit** — per user per day · per repo per hour · per chat single-call cost cap
- **Three cancel paths** — apply `cancel-openbot` label · comment `@openbot stop` · set `OPENBOT_KILL_SWITCH` env var
- **Output scanning** — trufflehog on every bot-authored PR / comment
- **Sandbox isolation** — Modal sandbox with no env access; fork PRs never run by default

## Eval system

OpenBot's eval system is a first-class part of the project. Every release runs through it; results land in `evals/results/`. We've designed it as a three-phase staircase that mirrors the bot's deployment maturity:

| Phase | Source of test data | Trigger to start |
|---|---|---|
| **v0.1 External** | Public benchmarks (SWE-Bench Verified, CodeReviewBench, SWE-QA) | Day one — directly use community datasets |
| **v0.2 Internal** | Curated samples from OpenBot's own production runs (dogfooding) | ≥ 200 real samples per task |
| **v0.3 Online** | External live benchmarks where they exist; production sampling where they don't | Bot deployed to ≥ 3 OSS repos |

Mapped to the pipeline:

| Task | v0.1 external | v0.2 internal (dogfooding) | v0.3 online |
|---|---|---|---|
| Triage | OSS-labeled issues (200) | Bot decisions + maintainer corrections | Internal production stream |
| Fix | [SWE-Bench Verified](https://www.swebench.com/) (500 tasks) | Bot PRs + merge / revert outcomes | [SWE-Bench Live](https://swe-bench-live.github.io/) (monthly +50, external) |
| Review | [CodeReviewBench](https://www.codereviewbench.com/) (50 PRs, offline) | Bot reviews + maintainer usefulness labels | [CodeReviewBench Online](https://www.codereviewbench.com/) (200k+ PRs daily, external) |
| Chat | [SWE-QA](https://arxiv.org/abs/2509.14635) (576 Qs) | Real `@bot` interactions + my-labeled correctness | Internal production stream |
| Safety | 24 prompt-injection prompts across 5 categories | Expanded to 100+ with real CVE patterns | Threat intel feed (OWASP LLM Top 10, MITRE ATLAS, CVE) |

The **dogfooding loop** is core: the bot's own runs on your repo become the next eval set — both for OpenBot upstream and for your own private fork. If you find OpenBot mislabeled an issue or wrote a bad review, that data point lands directly in `v0.2 internal` and helps the next release do better.

- Eval runner: [Inspect AI](https://inspect.aisi.org.uk/)
- Tracing / experiments: [LangSmith](https://www.langchain.com/langsmith)
- Full design: [`docs/prd/openbot-eval-prd.md`](./docs/prd/openbot-eval-prd.md)

## Status and roadmap

| Phase | Scope | Timeline |
|---|---|---|
| **Pre-alpha (now)** | Skeleton: webapp · config loader · GitHub adapter · persistence · LLM router · triage workflow stub | 2026-05 |
| **v0.1 alpha** | Full 5-stage pipeline + chat, GitHub-only · 4 safety mechanisms · 5 external eval suites | 4-6 weeks |
| **v0.2 MVP** | + Linear adapter · community plugin PRs · issue dedup · audit CLI · internal eval datasets | + 4-6 weeks |
| **v0.3+** | + Slack/Discord · web dashboard · plugin marketplace · live eval pipeline · optional hosted multi-tenant | + 2-3 months |

## Differentiators

| | OpenBot | Copilot Coding Agent | Devin | CodeRabbit | Sweep (EOL) |
|---|---|---|---|---|---|
| OSS / self-host | yes | no | no | partial | no |
| BYO API key | yes | no | no | partial | no |
| Multi-channel | yes (v0.2+) | no | yes | no | no |
| Plugin system | yes | no | no | rules-only | no |
| Multi-vendor LLM (LiteLLM) | yes | no | no | partial | no |
| Public eval suite | yes | no | partial | partial | no |
| Issue → PR loop | yes | yes | yes | no | yes |
| Long agent loop + persistent sandbox | yes (Modal per-thread) | partial (ephemeral runner) | yes | no | yes |

**Market position**: for OSS maintainers who refuse to be locked into a closed SaaS and want full control over prompt, model, data, and cost.

## Documentation

- [PRD](./docs/prd/openbot-prd.md) — full product spec, source of truth
- [Eval system PRD](./docs/prd/openbot-eval-prd.md) — benchmarks, runners, datasets
- [Config example](./docs/prd/openbot-config-example.yaml) — copy-paste starting config
- [Triage labels](./docs/agents/triage-labels.md) — label taxonomy
- [Research archive](./docs/research/) — design evolution, including the 80-question interrogation and prior PRD versions

## Development setup

### Secrets / env vars (Doppler-first)

OpenBot uses [Doppler](https://www.doppler.com/) for local / staging / prod env management. `.env` is the offline / CI fallback ([`./.env.example`](./.env.example) lists the full schema).

| Doppler project | Purpose | Owner |
|---|---|---|
| `openbot` (configs: `dev` / `stg` / `prd`) | OpenBot-specific: GitHub App · Modal · Postgres · Redis · R2 · kill switch | This repo |
| `infra` (config: `prd`) | Account-shared: `ANTHROPIC_API_KEY` · `OPENAI_API_KEY` · `LANGSMITH_API_KEY` | Cross-project |

Shared LLM tokens sync into `openbot` via an explicit allowlist in [`scripts/doppler-bootstrap-shared.sh`](./scripts/doppler-bootstrap-shared.sh) — re-run after each rotation.

One-time setup:

```bash
./scripts/doppler-bootstrap-shared.sh             # sync infra → openbot shared keys
doppler setup --project openbot --config dev      # bind repo to dev config
# fill in GitHub App / Modal values via dashboard or CLI
```

Daily:

```bash
doppler run -- uvicorn openbot.webapp:app --reload        # local dev
doppler run --config prd -- python -m openbot.worker      # worker (when implemented)
```

Offline fallback:

```bash
doppler secrets download --no-file --format env > .env
```

### Git hooks

Pre-commit / commit-msg / pre-push hooks are configured in [`.pre-commit-config.yaml`](./.pre-commit-config.yaml):

| Stage | Checks | Source |
|---|---|---|
| `pre-commit` | trailing-whitespace · large-files · `detect-private-key` · `ruff --fix` · `ruff-format` · trufflehog staged diff · block `.env`/`*.pem`/`*.key` | PRD §4.8 / §8.4 |
| `commit-msg` | Conventional commit format (`feat` / `fix` / `chore` ...) | PRD §8.4 |
| `pre-push` | trufflehog full-history · `pytest -x` (unit + integration, excludes `evals/`) | PRD §8.3 / §8.4 |

Install once:

```bash
./scripts/install-hooks.sh
```

Intentionally **not** in hooks (run via CI / cron instead): mypy, bandit, coverage gate, LLM evals — these would slow local commits.

### Make targets

```bash
make sync     # uv sync --dev  (never use pip install)
make fmt      # ruff format
make lint     # ruff check
make test     # pytest, excludes evals/
make check    # fmt-check + lint + test  ← run before every commit
make dev      # uvicorn with autoreload
make hooks    # install git hooks
```

Eval workflows intentionally live in their own Makefile:

```bash
make -C evals help          # discover eval-only targets
make -C evals data          # safely publish the current eval datasets
make -C evals data-refresh  # force-refresh all eval datasets
make -C evals test          # run tests/eval only
make -C evals smoke         # run the four implemented live smoke evals
make -C evals check         # eval tests, then live smoke evals
make -C evals view-open     # bundle eval logs, serve them, and open Inspect View
```

See [`evals/README.md`](./evals/README.md) for the task map, sandbox boundary, and per-surface targets.

## Contributing

v0.1 implementation is in progress. Watch the repo for `CONTRIBUTING.md` arriving with the first end-to-end skeleton.

To discuss design before code lands: open an issue tagged `discussion`. Triage labels are documented in [`docs/agents/triage-labels.md`](./docs/agents/triage-labels.md).

## License

[Apache-2.0](./LICENSE)
# E2E Test
# Fixed
