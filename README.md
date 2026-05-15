# OpenBot

> **Status: pre-alpha.** Spec is locked; implementation hasn't started yet.

An **open-source, self-hosted, BYO-API-key GitHub maintenance bot** for individual OSS maintainers.

You build your own GitHub App, run `docker compose up`, point it at your repo — 30 minutes from zero to a bot that triages issues, reviews PRs, fixes assigned issues, and answers `@mention` questions. Your API key, your data, your control.

## What it does (v0.1 MVP)

| Feature | Trigger | Default budget |
|---|---|---|
| **Triage** | `issue.opened` | $0.20 / issue — auto-labels + priority + reproduce attempt |
| **PR Review** | `pull_request.opened` / `.synchronize` | $0.50 / PR — severity-filtered inline review (never blocks merge) |
| **Issue → PR Fix** | assign issue to bot | $3.00 / task — sandbox agent loop, opens PR, never auto-merges |
| **@mention Chat** | comment with `@openbot ...` | $0.30 / call — read-only tool whitelist |

**Three-layer cost cap** + **three-layer rate limit** + **three cancel paths** (`cancel-openbot` label / `@openbot stop` / env kill switch) keep runaway costs and abuse contained.

## Differentiators

- **Open source · Apache-2.0 · self-hosted** — no SaaS lock-in
- **BYO LLM API key** — your tokens, your spend, your audit trail
- **Multi-channel architecture** — v0.1 GitHub only; v0.2 adds Linear; v0.3+ Slack/Discord/Web frontend
- **Plugin system** — drop a `@tool` into `openbot_plugins/`, send a PR; (v0.3) PyPI marketplace with sandboxed plugins
- **Multi-vendor LLM via LiteLLM** — Anthropic default; OpenAI / Gemini fallback configurable

## Status

| Phase | Scope | Timeline |
|---|---|---|
| **Pre-alpha (now)** | PRD locked, implementation not started | 2026-05 |
| **v0.1 alpha** | GitHub-only MVP — 4 features + 4 safety mechanisms | 4-6 weeks |
| **v0.2 MVP** | + Linear adapter + community plugin PR + issue dedup + audit CLI | + 4-6 weeks |
| **v0.3+** | + Slack/Discord + Next.js dashboard + PyPI plugin sandbox + optional hosted multi-tenant | + 2-3 months |

## Documentation

- **[Product Requirements Document](./docs/prd/openbot-prd.md)** — the source of truth for what OpenBot is and isn't
- **[Reference `.openbot/config.yaml`](./docs/prd/openbot-config-example.yaml)** — copy-paste starting config
- **[Research & evolution archive](./docs/research/)** — how the spec evolved from v0.1 to v0.3 (including the 80-question interrogation)

## Contributing

Implementation hasn't started. If you want to follow along or contribute once code lands, watch this repo — `CONTRIBUTING.md` will arrive with the first code drop.

### Git hooks（已就位）

仓库内置 `pre-commit` 配置（[`.pre-commit-config.yaml`](./.pre-commit-config.yaml)），覆盖：

| 阶段 | 检查 | 来源 |
|---|---|---|
| `pre-commit` | trailing-whitespace · large-files · `detect-private-key` · `ruff --fix` · `ruff-format` · **trufflehog staged diff** · 阻断 `.env`/`*.pem`/`*.key` | PRD §4.8 / §8.4 |
| `commit-msg` | conventional commit 类型（`feat` / `fix` / `chore` …） | PRD §8.4 `pr_lint.yml` |
| `pre-push` | **trufflehog full-history** · `pytest -x`（unit + integration，跳过 `evals/`） | PRD §8.3 / §8.4 |

一键安装：

```bash
./scripts/install-hooks.sh
```

刻意**不加**的检查（与 PRD §8.4 / §13 锁定决策一致）：mypy / bandit / coverage gate / LLM eval —— 这些走 CI / cron，避免本地 commit 被慢检查卡住。

## License

[Apache-2.0](./LICENSE)
