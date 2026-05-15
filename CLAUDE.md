# OpenBot

OpenBot 是一个开源、自托管的 GitHub 维护机器人（个人 OSS maintainer 场景）。详细产品规格见 [`docs/prd/openbot-prd.md`](./docs/prd/openbot-prd.md)；完整配置示例见 [`docs/prd/openbot-config-example.yaml`](./docs/prd/openbot-config-example.yaml)。

## Agent skills

### Issue tracker

Issues 和 PRD 通过 **GitHub Issues** 管理，用 `gh` CLI 操作。仓库尚未 `git init` 时，相关 skill 应先提示 `git init` + 配置 remote。详见 [`docs/agents/issue-tracker.md`](./docs/agents/issue-tracker.md)。

### Triage labels

采用 5 个默认 triage label：`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`。与 PRD 中的业务 label（`cancel-openbot` / `priority/P0..P3` / `bug` 等）并存，互不冲突。详见 [`docs/agents/triage-labels.md`](./docs/agents/triage-labels.md)。

### Domain docs

Single-context 布局：根目录一份 `CONTEXT.md` + `docs/adr/`（按需 lazily 创建，不存在时 skill 静默继续）。详见 [`docs/agents/domain.md`](./docs/agents/domain.md)。
