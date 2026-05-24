# OpenBot · Product Requirements Document

> 版本：**v0.1 Alpha Current-State** · 起草日期：2026-05-15 · 最近更新：2026-05-22 · 状态：开发中 / 不可标记为端到端可发布
> 历史演进：[v0.1](../research/openbot-prd-v0.1.md) · [v0.2](../research/openbot-prd-v0.2.md) · [v0.3](../research/openbot-prd-v0.3.md) · [80 问拷问清单](../research/openbot-interrogation.md)
> 配套：[完整 config 示例](./openbot-config-example.yaml) · [v0.1 收口 spec](../superpowers/specs/2026-05-22-v0-1-product-closure-design.md)

OpenBot 是一个 **开源、自托管、用户自带 LLM API key 的 GitHub 维护机器人**。每位 OSS maintainer 自建 GitHub App，运行一个完全属于自己的实例；OpenBot 项目本身不托管用户代码、不持有用户数据、不抽成 LLM 费用。v0.1 仅 GitHub channel，v0.2 起再接入 Linear，v0.3+ 扩展到 Slack / Discord / Web frontend。

这份 PRD 是当前执行文档，不是历史归档。它同时说明产品愿景、v0.1 alpha cutline、当前开发进度、未完成缺口和 alpha readiness gate。

---

## 目录

1. [Executive Summary](#1-executive-summary)
2. [产品定位与差异化](#2-产品定位与差异化)
3. [目标用户与非目标用户](#3-目标用户与非目标用户)
4. [v0.1 MVP 完整规格](#4-v01-mvp-完整规格-github-only)
5. [架构](#5-架构)
6. [配置](#6-配置)
7. [部署](#7-部署)
8. [Quality & Evaluation](#8-quality--evaluation)
9. [v0.2 规格](#9-v02-规格)
10. [v0.3+ 战略 Roadmap](#10-v03-战略-roadmap)
11. [成功指标](#11-成功指标)
12. [Risks & Mitigations](#12-risks--mitigations)
13. [关键决策（全部锁定）](#13-关键决策全部锁定)
14. [Glossary](#14-glossary)
15. [References](#15-references)

---

## 1. Executive Summary

OpenBot 帮助个人 OSS maintainer 把日常 GitHub 维护工作自动化：

| 功能 | 触发 | 默认预算 | v0.1 alpha cutline |
|---|---|---|---|
| **Triage** | `issue.opened` | $0.20 / issue | label + priority + bounded clarification/reproduce evidence |
| **PR Review** | `pull_request.opened` / `.synchronize` | $0.50 / PR | severity-filtered PR review, advisory only |
| **Issue → PR Fix** | assign issue 给 bot | $3.00 / task | sandbox-backed patch + tests + branch + PR |
| **@mention Chat** | 评论中 `@openbot ...` | $0.30 / 次 | read-only repo-grounded answer or refusal |

四个功能都跑在用户自建的 GitHub App + 用户自有 LLM API key 上。OpenBot 项目本身既不收钱也不持有用户数据。

**安全与成本的三层防护**（v0.1 alpha 必保留）：

- **三层 cost cap**：per-task 硬限 + 每 repo 每月软上限 + 全实例月度硬 kill
- **三层 rate limit**：每用户每日 / 每 repo 每小时 / 单次 chat cost cap
- **三种取消**：`cancel-openbot` label / 评论 `@openbot stop` / `OPENBOT_KILL_SWITCH` env
- **Trufflehog 输出扫描 + Daytona sandbox env 隔离 + fork PR 默认不跑**

**差异化**：开源 · 自托管 · 自带 API key · 多 channel 架构预留 · 插件系统 · 多 vendor LLM。

**v0.1 不做**：Linear、社区 plugin、Issue dedup、Web frontend、自动 merge、多租户托管。

### 1.1 当前开发进度

| Area | 当前状态 | Alpha 前必须完成 |
|---|---|---|
| GitHub ingress | FastAPI route、GitHubAdapter、签名校验、dedup、router 已有实现 | 统一 webhook -> queue -> worker 的 payload contract |
| Queue / worker | Redis Stream worker、retry、DLQ、TaskSpec v3 已有实现 | API 入站必须产出 worker 消费的 `TaskSpec v3`，不能继续产出旧 `QueuePayload` |
| Preflight | sanitize、kill switch、feature toggle、cancel、fork PR、actor role、rate limit、budget、audit-start 已成链 | 确保生产入口必经 preflight，测试不再依赖旧入口 |
| Review | 结构化 findings、severity filtering、PR Review API 写回已接近目标 | 补 bot 输出 secret scan、增量 review 元数据闭环 |
| Fix | use case 已能消费 sandbox handle 并组织 DeepAgents -> branch -> PR | worker 注入 sandbox factory；真实路径不应默认降级为 no-sandbox |
| Triage | 当前只有 ACK | 实现 label + priority；reproduce 作为条件触发而非无条件目标 |
| Chat | help/cancel 解析已存在；freeform agent 无工具 | 加 read-only repo tools 与状态变更拒绝规则 |
| Cost / cancel | 月度/global preflight 已有；fix 有若干 checkpoint | per-task budget 与 agent-loop cancel 进入 DeepAgents runtime |
| Eval | review/fix/test/chat offline eval surfaces 已实现 | 与 alpha gate 连接；昂贵 full eval 仍不进同步 CI |
| Docs | README/PRD/部署文档存在 | 统一 Daytona vs Modal、GLM vs Claude 默认值、当前状态 |

### 1.2 v0.1 Alpha Readiness

v0.1 alpha 只有在以下条件全部满足后才可标记为 dogfoodable：

1. `make check` 通过。
2. `make -C evals test` 通过。
3. 本地 signed webhook smoke 覆盖 issue opened、PR opened、issue assigned、`@openbot help`、`@openbot stop`、一个 repo-grounded chat。
4. Webapp 产生的队列 payload 类型与 worker 消费类型完全一致。
5. Fix 在配置 Daytona 后能进入 sandbox-backed path；未配置时有清晰降级。
6. per-task、monthly、global budget gate 均有 enforcement-point 测试。
7. bot-authored output 在测试中不能泄漏 synthetic secret。
8. README、PRD、部署文档不再互相矛盾。

---

## 2. 产品定位与差异化

### 2.1 一句话定位

> "你自己的 GitHub maintainer 副驾驶 —— 完全 OSS、完全自托管、完全属于你的 API key 和数据。"

### 2.2 锁定的 15 项根基决策

| # | 维度 | 决策 |
|---|---|---|
| 1 | 项目性质 | OSS 项目，Apache-2.0 |
| 2 | 仓库名 | `openbot` |
| 3 | 目标用户 | 个人 OSS maintainer（1-2 repo） |
| 4 | 差异化轴 | 开源 + 可插拔 + 多 channel + 自带 API key |
| 5 | Bot 身份 | GitHub App，**每用户自建** |
| 6 | 租户模型 | 单租户 self-host（v1.0+ 可选托管多租户） |
| 7 | Channel 策略 | v0.1 仅 GitHub；ChannelAdapter ABC 自 day-1 抽象；v0.2 加 Linear |
| 8 | MVP 功能 | Triage + Review + Fix + Chat 四个全上 |
| 9 | 插件 | LangGraph tool；v0.2 起 in-tree PR；v0.3 开 PyPI |
| 10 | LLM 接入 | LiteLLM 多 vendor 抽象；当前默认 `anthropic/GLM-5.1`，可由 `.openbot/config.yaml` 覆盖 |
| 11 | Entry 架构 | ChannelAdapter ABC（GitHubAdapter v0.1，LinearAdapter v0.2） |
| 12 | 触发模型 | review/triage 自动；fix 用 issue assign；chat 用 `@openbot` |
| 13 | 配置 | 仓库内 `.openbot/config.yaml`（PR-able、可审计） |
| 14 | Sandbox | v0.1 当前实现为 Daytona；sandbox port 保持可插拔，Modal / local 为后续后端 |
| 15 | 滥用与成本防护 | 三层 cost cap + 三层 rate limit + 三种 cancel |

### 2.3 与同类工具的差异

| 维度 | OpenBot | Copilot Coding Agent | Devin | CodeRabbit | Sweep (已停服) |
|---|---|---|---|---|---|
| OSS / 自托管 | ✅ | ❌ | ❌ | 🟡 OSS PR review 免费 | ❌ |
| 自带 API key | ✅ | ❌ | ❌ | 🟡 部分 | ❌ |
| 多 channel | ✅ (v0.2+) | ❌ | ✅ 一流 | ❌ | ❌ |
| 插件系统 | ✅ 仓库 plugin + (v0.3) PyPI | ❌ | ❌ | 🟡 .yaml 规则 | ❌ |
| 多 vendor LLM | ✅ LiteLLM | ❌ | ❌ | 🟡 | ❌ |
| Issue → PR 全自动 | ✅ | ✅ | ✅ | ❌ | ✅ |
| 长 agent loop + 持久 sandbox | ✅ Daytona per-task（后续可换后端） | 🟡 GHA runner ephemeral | ✅ | ❌ | ✅ |

**OpenBot 的市场缝隙**：给「不想被任何闭源 SaaS 绑架、希望完全控制 prompt / model / 数据 / 成本」的个人 OSS maintainer 用。

---

## 3. 目标用户与非目标用户

**目标用户**：

- 个人 OSS maintainer，维护 1-2 个公开 repo
- 每周收到几个到几十个 issue / PR
- 已经在用 Claude / GPT / Gemini 写代码
- 愿意花 30 分钟读 README 自建 GitHub App + 跑 docker-compose
- 不需要 SSO、多租户、SOC2 等企业能力

**非目标用户（v1.0 之前）**：

- 大型企业团队 → 用 Copilot Enterprise / CodeRabbit Pro / Devin
- 完全无技术背景的项目主 → OpenBot 需要懂 docker / GitHub App
- 想要 zero-config 即开即用 → 用 Copilot Coding Agent 或 Claude Code Action

---

## 4. v0.1 MVP 完整规格 (GitHub-only)

> 工期：4-6 周 alpha · 范围：4 功能 + 4 类防护机制。v0.1 的目标是 dogfoodable，不是功能终态。

### 4.0 v0.1 Cutline

| Feature | Must ship in alpha | May degrade in alpha | Deferred |
|---|---|---|---|
| Triage | label、priority、低成本说明/澄清 | 仅当 issue 有明确 repro 信息时进入 sandbox reproduce | dedup、自动 close、复杂复现矩阵 |
| Review | diff-based structured findings、severity filter、PR Review API、advisory only | 大 PR 可降级为摘要 review | blocking check、自动 merge |
| Fix | authorized assignment、Daytona sandbox、patch/test/push/open PR | CI failure self-fix 可先不做或限 0 次 | 3 次 CI self-fix、warm sandbox cache |
| Chat | help/cancel、read-only repo-grounded answer、状态变更拒绝 | `web_fetch` 可先关闭 | 写文件、开 PR、merge、任意 shell |
| Safety | kill switch、cancel label/comment、fork PR default-deny、output scan | config approval 可先以 preflight check 形式落地 | full plugin trust model |

### 4.1 功能：Triage

- **触发**：`issue.opened` webhook（可关）
- **预算**：默认 `$0.20 / issue`（per_task hard cap）
- **可取消**：`cancel-openbot` label 加到 issue 上立即停

Alpha pipeline：(1) 从 `triage.labels.available` + repo 现有 label 中选 1-3 个 → (2) 打 `priority/P0` ~ `priority/P3` label → (3) 如果 issue 是 bug 且已有明确复现步骤，进入 Daytona sandbox 做 bounded reproduce 并把简短证据贴回 issue；否则发澄清/说明或只打标签。

非目标：v0.1 不做 issue dedup、不自动 close、不学习 maintainer 私有 triage policy。

### 4.2 功能：PR Review

- **触发**：`pull_request.opened` + `.synchronize`
- **预算**：默认 `$0.50 / PR`
- **可取消**：`cancel-openbot` label

Pipeline：拉 diff（不拉完整文件） → LLM 输出结构化 finding 列表（含 severity） → **按 `severity_threshold` 过滤**（默认 `medium`，丢弃 low/nit） → 一条摘要评论 + 若干 inline 评论 → 后续 push 增量 review (`multi_turn: true`)。

**OpenBot 永不 merge，只 propose。** PR check 默认 `blocking: false`。

### 4.3 功能：Issue → PR Fix

- **触发**：issue assignee 含 bot（仅 `collaborator` / `owner` 可 assign，配置 `fix.allowed_actors`）
- **预算**：默认 `$3.00 / task`（最高的）
- **可取消**：`cancel-openbot` label / `@openbot stop` 评论 / global kill

Agent loop：(1) 即时 ACK 评论 + LangSmith trace URL → (2) Daytona sandbox + clone repo → (3) DeepAgent loop：`read → write patch → run test → self-fix` → (4) push `openbot/fix-issue-{issue_num}-{sha}` 到主 repo → (5) 开 PR (`draft: false`) → (6) **永远不 auto-merge**。

**Limits**：`max_steps: 80`，`max_wall_seconds: 2700`（45 min），`max_cost_usd: 3.00`。CI 失败 self-fix 最多 3 次是 v0.1+ enhancement，不是 alpha readiness gate。

### 4.4 功能：@mention Chat

- **触发**：评论包含 `@openbot <自然语言>`，默认 `allow_anyone: true`
- **预算**：`$0.30 / 次`
- **可取消**：`@openbot stop`

工具白名单（**read-only**）：`read_file` / `glob` / `grep`。`shell_readonly`、`web_fetch`、`search_linked_issues`、`search_linked_prs` 是 v0.1+ 可选增强，只有在 SSRF / allowlist / output budget 都落地后启用。禁用：`write_file` / `shell_write` / `gh_pr_create` / `gh_pr_merge`。

Chat agent **不能改任何状态**。要触发改动，让它建议动作，再由人触发其他 workflow。

### 4.5 三层 Cost Cap

```yaml
budget:
  per_task: { triage: 0.20, review: 0.50, fix: 3.00, chat: 0.30 }
  monthly_soft_cap_usd: 100          # 锁定：每 repo 每月 $100
  monthly_alert_at_pct: 80           # 用了 80% alert admin
  global_hard_kill_usd: 500          # 锁定：每实例每月 $500
```

实现要点：
- 每次 LiteLLM 调用完成后，把 cost 写入 Postgres `cost_meter`（task_id / repo / feature / cost / ts）
- middleware `BudgetEnforcement` 在每 step 之前查累计 cost
- 超 `per_task` → 优雅停止 + 评论 "Hit per-task budget"
- 超 `monthly_soft_cap` → 该 repo 当月剩余 workflow 跳过 + admin email
- 超 `global_hard_kill` → **整个 worker 池停 dequeue**，admin 跑 `openbot budget reset` 才恢复
- env 覆盖：`OPENBOT_GLOBAL_HARD_KILL_USD=500`

当前实现状态：

| Enforcement point | 状态 |
|---|---|
| `cost_meter` schema | 已实现 |
| 单次 LiteLLM wrapper cost record | 已实现，但 DeepAgents runtime 需要统一接入 |
| monthly repo soft cap preflight | 已实现 |
| global hard kill preflight | 已实现 |
| per-task agent-loop cap | 未完成，alpha 前必须进入 runtime |
| admin email / `budget reset` | v0.2 audit CLI 范围 |

### 4.6 三层 Rate Limit

```yaml
chat.rate_limit:
  per_user_per_day: 20
  per_repo_per_hour: 100
  cost_cap_per_task: 0.30
  exempt_roles: [owner, collaborator]   # 锁定：collaborator 默认 exempt
```

Redis 计数器：`rl:user:{user_id}:{YYYY-MM-DD}` 与 `rl:repo:{repo_id}:{YYYY-MM-DD-HH}`。超限 → bot 评论 `Rate limited: 20/20 daily uses reached. Resets at 00:00 UTC.`

当前实现状态：chat feature 的 user/day 与 repo/hour preflight gate 已实现；`cost_cap_per_task` 需要与 §4.5 的 per-task budget 一起在 runtime 层 enforcement。

### 4.7 三种取消机制

| 方式 | 触发 | 适用 | 响应延迟 |
|---|---|---|---|
| **Label** | issue/PR 上加 `cancel-openbot` | fix / triage / review | 5 step 内（≈ 30s） |
| **Comment** | 评论 `@openbot stop` / `cancel` / `停` / `取消` | chat task | 立即 |
| **Env kill** | `OPENBOT_KILL_SWITCH=true` | 紧急停整个实例 | 立即（worker 下一 step 退出） |

```python
# openbot.application.middleware/cancellation.py（核心逻辑）
async def check_cancellation_before_step(state, runtime):
    if os.environ.get("OPENBOT_KILL_SWITCH") == "true":
        return STOP_WITH_MESSAGE("Global kill switch engaged")
    if await redis.sismember(f"cancel:thread:{state.thread_id}", "1"):
        return STOP_WITH_MESSAGE("Cancelled by user")
    if state.step_num % 5 == 0:                  # 5 step 一次，省 GitHub API
        labels = await gh_api.get_issue_labels(state.repo_id, state.issue_num)
        if "cancel-openbot" in labels:
            return STOP_WITH_MESSAGE("Cancelled by label")
    return CONTINUE
```

当前实现状态：env kill、cancel label、cancel comment 的 input-side gate 已有；长 agent loop 中还需要统一 runtime checkpoint，确保 review/fix/chat 不会在取消后继续消耗 LLM 或写回 GitHub。

### 4.8 安全 & 滥用防护

| 防护 | 实现 |
|---|---|
| **Fork PR 默认不跑** | `security.fork_pr.run: false`；要 maintainer 评论 `/ok-to-test` 才放行 |
| **Prompt injection 防护** | 用户内容用结构化边界包裹；system prompt 显式声明忽略用户内容内的指令变更 |
| **Secret 扫描** | Bot 评论 / review / PR body 发出前过 egress scanner；命中即 redact 或 safe fallback |
| **Sandbox env 隔离** | Daytona sandbox 不继承宿主 env，只注入 task 所需短期 GitHub App installation token |
| **Config 改动审批** | `.openbot/config.yaml` 的 PR 改 budget / allowed_tools 等高风险字段时，需 admin 加 `config-approved` label 才生效；alpha 可先以 preflight gate 落地 |
| **审计 log** | Postgres `audit_log` 全记录（trigger / actor / feature / cost / outcome）；查询 CLI 完整版 v0.2 上 |
| **GitHub App 最小权限** | install 时 scope 按功能开关动态生成 |

当前实现状态：fork PR gate、actor role gate、audit log schema 已有；bot-authored output egress scanning、config approval gate、动态 GitHub App permission 仍未完成。

---

## 5. 架构

### 5.1 v0.1 数据流

```
GitHub App webhook
        ▼
Ingress (FastAPI)
  verify signature · dedup delivery_id · 立即 202 · build TaskSpec v3 · enqueue Redis
        ▼
ChannelAdapter (ABC)
  GitHubAdapter (v0.1) · LinearAdapter (v0.2 stub)
        ▼ UnifiedEvent
Router + Pre-flight (load config · budget · rate · kill switch · dispatch)
        ▼
Triage | Review | Fix | Chat workflow
        ▼
DeepAgents runtime
  middleware stack (顺序敏感):
    1. SanitizeInputs  → 2. CallLimit  → 3. ToolError
    4. BudgetEnforce   → 5. CancelCheck → 6. MessageQueue
    7. CircuitBreaker  → 8. ModelFallback
        ▼
Daytona Sandbox (per-task; backend is pluggable)
        ▼
Storage: Postgres (audit_log, cost_meter, thread_meta, rate_limit_counter)
         Redis (queue, rate, dedup)
         R2 / local FS (artifacts)
         LangSmith (trace + cost)
```

### 5.2 关键架构决策

- **代码起点**：LangChain Open SWE 骨架，移植 + 重命名 module 为 `openbot/`
- **Graph factory pattern**：`get_agent(config)` 每 thread 实例化一次，注入 sandbox / token / prompt
- **Queue contract**：v0.1 alpha 统一为 `TaskSpec v3`；webapp 不能再产出 worker 不消费的旧 payload
- **Sandbox 生命周期**：worker composition root 注入 sandbox factory；handler 只消费 `SandboxedHandle | None`
- **Middleware 顺序敏感**：上图自上而下严格按顺序
- **Thread metadata 是真相之源**：sandbox ID、加密 OAuth token、findings、message queue 全活在 LangGraph store 上，不依赖外部 secret store
- **Webhook 永不阻塞**：立即 202 + 异步 enqueue

### 5.3 v0.2 架构增量

LinearAdapter 复用同一 ChannelAdapter ABC 接口 —— **不需要重画上面这张图**，只是多一个 adapter 实现 + Router 多一个分支。

---

## 6. 配置

完整 config 见 **[`./openbot-config-example.yaml`](./openbot-config-example.yaml)**。关键字段：

- `model.per_feature.*` —— 当前默认 `anthropic/GLM-5.1`（可通过 Anthropic-compatible proxy 路由）；release 推荐模型可由配置覆盖
- `budget.*` —— 三层 cost cap（per_task / monthly_soft_cap_usd / global_hard_kill_usd）
- `*.rate_limit.exempt_roles: [owner, collaborator]` —— collaborator 默认 exempt
- `cancel.label: cancel-openbot` —— 锁定 cancel 触发词
- `comment_language: auto` —— 跟随 issue 语言
- `sandbox.backend: daytona` / `OPENBOT_DAYTONA_API_KEY` —— v0.1 当前实现的 production sandbox
- `plugins.builtin: [reproduce_python_issue, reproduce_js_issue, summarize_pr_diff]` —— v0.2 起作为社区 plugin 示例完善
- `storage.artifacts.backend: r2` / `fallback: local_fs` —— 不强依赖 R2

仓库内 `.openbot/config.yaml` 是单一来源；改 config 走 PR，可审计、可回滚。

---

## 7. 部署

部署路径分四类，不再用一个“30 分钟上线”覆盖所有环境：

| Path | 用途 | 状态 |
|---|---|---|
| Native local dev | 日常开发、测试、debug | 当前主路径 |
| Docker compose | self-host alpha 目标路径 | 需要在 v0.1 closure 中重新验证 |
| Heroku + Doppler | dogfood / preview ops | runbook 已有，需跟 Daytona/队列状态同步 |
| Docker Hub image | 外部用户低摩擦安装 | alpha 后发布，不是当前阻塞项 |

目标 self-host 流程：

```bash
git clone https://github.com/<you>/openbot && cd openbot
./setup.sh
# 1. 浏览器引导你创建 GitHub App（自动填好 permissions / events / callback）
# 2. 填 .env: App ID / private key / webhook secret / LLM key / Daytona key / LangSmith key
# 3. docker compose up -d
# 4. 浏览器去 GitHub install App 到 repo
# 5. 在 repo 加 .openbot/config.yaml（仓库自带模板）
```

外部依赖：Docker + docker-compose v2 · Postgres 16 · Redis 7 · Daytona account · LLM API key · LangSmith key · (可选) Cloudflare R2。

**Docker Hub**：v0.1 alpha dogfood 通过后 publish `openbot/openbot:v0.1.x`，不强制 build from source。

---

## 8. Quality & Evaluation

完整 spec 见 [eval PRD](./openbot-eval-prd.md) 与 [eval suite 定义](./openbot-eval-suites.md)；本节给出主 PRD 视角的浓缩摘要，详细目录、删除清单、LangSmith 契约和 suite 规则留在 eval PRD。

> 浓缩自 `CICD_AND_EVALS_CN.md` / `eval-setup-recommendation.md` / `github-bot-evaluation-benchmarks.md`。

### 8.1 评估框架选型

| 角色 | 选型 | 理由 |
|---|---|---|
| **Eval runner** | **Inspect AI**（UK AISI 开源） | 负责 dataset / task / solver adapter / scorer 调度；不拥有 OpenBot agent 或 sandbox |
| **被测系统入口** | `openbot.evaluation` facade | evals 通过产品侧 facade 调用真实 OpenBot harness、agent、sandbox、repo checkout |
| **Trace / 观测** | **LangSmith**（主选）+ Langfuse self-hosted（可选替代） | 与 LangChain / LangGraph 原生集成，dataset / experiment / online eval / annotation queue 一体化；若后续更重视自托管，再切 Langfuse |
| **判分** | golden set + LLM-as-judge + official harness / prediction export | 文本相似度对代码无效；patch 类 benchmark 以官方 harness 为最终成绩 |

**明确反对**：BLEU / ROUGE 用于评 code；以及在 `evals/` 里维护一套 eval-only agent / sandbox。

### 8.2 Benchmark 套件（v0.1 当前基线）

| Suite | Dataset / benchmark | 用途 | v0.1 状态 |
|---|---|---|---|
| `review_martian` | Martian Code Review Bench mirror | review `mean_f1` / precision / recall | 保留并改为调用 `openbot.evaluation.run_review_sample` |
| `fix_swe_bench` | SWE-bench Verified | valid prediction export；official `pass@1` offline | 保留并改为调用 `openbot.evaluation.run_fix_sample` |
| `chat_swe_qa` | SWE-QA-Pro mirror | chat 5-dim normalized judge | 保留并改为调用 `openbot.evaluation.run_chat_sample` |
| `test_swt_bench` | SWT-Bench Verified | test-generation diagnostic | surface 保留；产品能力未实现前输出 `unsupported=true` |
| `triage_gitbugs` | GitBugs subset | triage `macro_f1` | v0.2 candidate；等产品 triage 输出闭环 |
| internal suites | curated historical PR / issue / chat | 真实产品分布 | v0.2 解冻后 |

**Verified 的诚实声明**：公开 benchmark 用于可比性和回归趋势；真实产品质量还需要 internal curated set、dogfood 和 online signals。

### 8.3 测试分层

| 层 | 目标 | 工具 | 占比 |
|---|---|---|---|
| **Unit** | 纯函数、parser、数据结构 | pytest，无 monkeypatch env | ~70% |
| **Integration** | webhook → business logic | FastAPI TestClient + mock 下游 | ~20% |
| **Middleware** | budget / cancel / rate / state recovery 场景 | fake state machine + mock handler | ~7% |
| **Security** | SSRF / prompt injection / auth | assertion-error fake + positive case | ~3% |
| **Eval** | LLM 行为质量 | `evals/` 独立目录，CI 外手动触发 | — |

**昂贵 full eval 不进同步 CI**。当前策略是：
prompt / workflow / harness 改动 → 一条 smoke + 异步 regression；release 跑当前 phase 的公开 suite。详细 gate / budget / online 规则见独立 [eval PRD](./openbot-eval-prd.md)。

### 8.4 CI/CD Pipeline

| Workflow | 触发 | 内容 |
|---|---|---|
| `ci.yml` | push main + 所有 PR | `ruff lint` + `ruff format --check` + `pytest` 并行 |
| `pr_lint.yml` | PR opened / edited | PR title 强制 conventional commit |
| `promote.yml` | daily cron 08:00 UTC | main → `release/*` 分支（触发 Docker image build + push 到 Docker Hub） |
| `dependabot.yml` | monthly | uv / Docker base / actions 自动 PR |

**设计原则**：CI 只做 gating，**不做 deploy**；`concurrency: cancel-in-progress: true` 省 CI 分钟；不在 CI 跑 eval；暂不加 mypy / bandit / coverage gate —— 只 block critical，其余给 eval 兜底。

### 8.5 SLO & Quality Gates

| Gate | 指标 | 触发 | 行为 |
|---|---|---|---|
| Runner health | task import + one-sample smoke | eval runtime / harness 改动 | block eval refactor merge |
| LangSmith upload | experiment row + feedback 存在 | eval runtime / scorer 改动 | block eval refactor merge |
| Regression（soft） | review `mean_f1` ↓ ≥ 5% | 每次 review prompt 改 | PR 评论警告 |
| Regression（hard） | review `mean_f1` ↓ ≥ 10% | 同上 | block merge |
| Fix export | `fix_swe_bench` valid prediction JSONL | 每次 fix workflow / harness 改 | block eval runner merge |
| SWT diagnostic | `test_swt_bench` `unsupported=true` until product capability exists | 每周 / release | block if old eval-only agent is used |
| Chat regression | `chat_swe_qa` normalized_overall ↓ ≥ 5% | 每次 chat workflow / prompt 改 | warn |
| Review 延迟 | per-PR p95 ≤ 60 秒 | 实时 | 超 120s canary alert |
| Review 精度 | Martian `mean_f1` ≥ 0.55 | release | 趋势告警 + public dashboard |
| Comment 信噪 | 仅发 severity ≥ medium | 每条 | 自动过滤 low/nit |
| Prompt injection 防御 | 20 个 "Comment & Control" 攻击 case 全 fail-safe | 每 release | block release |

---

## 9. v0.2 规格

> v0.1 alpha dogfood 通过后再解冻。当前不作为 alpha acceptance surface。

**9.1 LinearAdapter** —— 完整实现 `ChannelAdapter` 接口；Linear issue 触发 → fix workflow → 在 GitHub 开 PR → fix 完成回写 Linear comment；Linear OAuth token 加密存 Postgres `channel_credentials`。

**9.2 社区 in-tree Plugin PR** —— 允许社区往 `openbot_plugins/` 提 PR 加新 plugin，仍跑主进程（trust model = 仓库 maintainer 信任）。v0.3 才上 PyPI 沙箱。

贡献流程（v0.2 写入 `CONTRIBUTING.md`）：fork → 在 `openbot_plugins/<name>.py` 实现 `@tool` → 单元测试 `tests/plugins/test_<name>.py`（**强制**，CI gate）→ 文档 `docs/plugins/<name>.md` → 开 PR。

Plugin PR review checklist：

- [ ] 没有外部网络调用（除 web_fetch 白名单）
- [ ] 不读 `os.environ`（防泄漏 secret）
- [ ] 不写文件到 sandbox 外
- [ ] 单元测试覆盖率 ≥ 80%（锁定为硬性要求）
- [ ] tool docstring 完整（LLM 靠 docstring 决定调用）

**9.3 Issue Dedup** —— Embedding via Voyage（fallback: OpenAI text-embedding-3-large） + pgvector + LLM rerank（top-10 → Sonnet 判语义重复）；新 issue → 找出 top-3 候选 → 评论里给 maintainer 决策；**永不自动 close**，只 propose。

**9.4 `openbot audit` CLI** —— `audit list --since=7d --feature=fix` / `audit show <task_id>` / `audit export --format=csv` / `budget reset`。v0.1 只要求审计数据写入；完整查询 UX 留 v0.2。

**9.5 Docs site** —— `mkdocs-material`（锁定：相比 Docusaurus 更对齐 Python 生态），GitHub Pages 部署；覆盖 install / config / 4 features / plugin authoring / FAQ。

---

## 10. v0.3+ 战略 Roadmap

> v0.2 之后 2-3 月，多个并行流。这里是方向，不参与 v0.1/v0.2 acceptance。

| 里程碑 | 内容 | 价值 |
|---|---|---|
| **Slack adapter** | 第三个 ChannelAdapter，对齐 Devin 的 Slack-first 体验 | 团队场景 |
| **Discord adapter** | OSS 社区主战场 | Community fit |
| **Next.js Web frontend** | dashboard / audit log / budget viewer / config editor | 降低运维门槛 |
| **PyPI plugin** | 第三方 plugin 跑独立 sandbox（trust 模型升级） | plugin marketplace 起步 |
| **多租户托管（可选）** | OpenBot Cloud：托管版给"懒得自己运维"的 maintainer，Apache-2.0 保持 | 项目可持续 |
| **Issue 分类 / Reproducer evals** | GitBugs + LIBRO 进 release gate | 质量护城河 |

**v1.0 目标**：100+ stars · 50+ 活跃 install · 3+ 外部 community contributor 合并过 plugin/feature PR · 完整 docs site（≥ 20 篇）· Plugin marketplace v1（PyPI + sandbox）。

---

## 11. 成功指标

### 11.1 Engineering Readiness（v0.1 alpha gate）

| Metric | Target |
|---|---|
| Webhook → queue → worker → handler | 同一 payload contract，端到端 smoke 通过 |
| Four workflow smoke | triage / review / fix / chat 均有 fake 或 live smoke |
| Test suite | `make check` 通过 |
| Eval tests | `make -C evals test` 通过 |
| Budget enforcement | per-task / monthly / global gate 均有测试 |
| Cancellation | label / comment / env kill / supersede checkpoint 均有测试 |
| Output secret safety | synthetic secret 不会进入 bot-authored GitHub output |
| Docs alignment | README / PRD / deploy docs 不再冲突 |

### 11.2 Eval Quality（v0.1 alpha target）

| Metric | Target |
|---|---|
| `review_martian` one-sample smoke | 通过并上传 LangSmith feedback |
| `fix_swe_bench` one-sample smoke | 产出 valid SWE prediction JSONL |
| `chat_swe_qa` one-sample smoke | 产出 answer + judge feedback |
| `test_swt_bench` one-sample smoke | 产出 `unsupported=true` metadata |
| `rg "evals\\.agents|evals\\.sandboxes|deepagents_baseline" evals tests/eval` | 无 live hits |
| Prompt injection defense | release 前红队 case 全 fail-safe |

### 11.3 Dogfood / Adoption（alpha 后观察）

| Metric | Target |
|---|---|
| Dogfood 在自家 repo 跑天数 | ≥ 7 |
| GitHub stars | ≥ 50 |
| 外部 install 数 | ≥ 5 |
| 平均 fix task 成本 | ≤ $2.00 |
| 单 task budget 卡住率 | < 5%（不能太敏感） |
| Bot 评论 👍 : 👎 比例 | ≥ 2 : 1 |

### 11.4 v0.2 完整 MVP（再 6 周）

| Metric | Target |
|---|---|
| Install 数 | ≥ 50 |
| Stars | ≥ 100 |
| External plugin PR merged | ≥ 1 |
| Linear install | ≥ 5 |
| SWE-bench Verified pass@1 (Sonnet 4.6) | ≥ 50% |
| Dedup Recall@10 | ≥ 0.65 |

---

## 12. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| LLM cost 失控 | ~~High~~ → **Low** | High | ✅ 三层 cost cap（§4.5） |
| Chat 被滥用 | ~~High~~ → **Low** | Medium | ✅ 三层 rate limit（§4.6） |
| Bot 跑错任务无法停 | ~~High~~ → **Low** | Medium | ✅ Label / comment / env kill 三种 cancel（§4.7） |
| Prompt injection 漏 token | Medium | Critical | Trufflehog 输出扫 + XML 包裹 user content + 红队回归 |
| Bot 评论太 noisy | Medium | Medium | severity threshold ≥ medium + 👎 监控 + §11 评论比例 SLO |
| Daytona 限流 / 故障 | Medium | High | Sandbox 后端抽象，预留 Modal / local 后端 |
| Anthropic API 限流 | Medium | High | LiteLLM fallback to OpenAI gpt-5-mini |
| GitHub App 每用户自建 onboarding 痛 | High | Medium | 详细 `setup.sh` wizard + 视频 + Docker Hub image |
| v0.1 6 周工期超 | Medium | Medium | 强收 scope；若超期砍 chat MVP，留 v0.2 |
| OSS community 不来 | Medium | Medium | v0.2 起 plugin PR + 详细 CONTRIBUTING.md + 3 个内置示例 plugin |
| 与 OpenHands / Copilot 同质化 | High | Medium | 死守"OSS + 多 channel + plugin + 自带 key"四轴差异化 |

---

## 13. 关键决策（全部锁定）

| # | 决策项 | **值** | 理由摘要 |
|---|---|---|---|
| 1 | 项目仓库名 | `openbot` | 简洁、可记、未被占用 |
| 2 | 默认 LLM 路由 | 当前代码默认 `anthropic/GLM-5.1`；用户可用 `.openbot/config.yaml` 覆盖 per-feature model | 与当前实现一致；release 推荐模型可按 eval 结果调整 |
| 3 | Artifact 存储 | R2 默认，**local FS fallback 允许** | 不强依赖 Cloudflare 账号 |
| 4 | Rate limit 对 collaborator | **默认 exempt** | 维护者不应被自家 bot 卡 |
| 5 | Cancel label 名 | `cancel-openbot` | 显式、可搜索、不冲突 |
| 6 | Global hard kill 默认值 | **$500 / month / instance** | 足够 ~150 个 fix task，单人场景充足 |
| 7 | Monthly soft cap 默认值 | **$100 / repo** | 控单仓库爆炸，$80 alert |
| 8 | Bot 评论语言 | **LLM 自动判断，跟随 issue 语言** | 国际化零配置 |
| 9 | Sandbox backend | v0.1 当前实现 Daytona；后端通过 `SandboxPort` 可插拔 | 代码、部署和 app.json 已 Daytona-first |
| 10 | Docs 站 | **mkdocs-material** | Python 友好、theme 成熟、零 JS 依赖 |
| 11 | Docker Hub 发布 | alpha dogfood 通过后发布 | 先保证真实可运行，再降低 onboarding 摩擦 |
| 12 | Plugin PR 单测必填 | v0.2 起强制，CONTRIBUTING.md 明确 | trust 模型的最小代价 |

---

## 14. Glossary

| 术语 | 含义 |
|---|---|
| **ChannelAdapter** | 抽象层，把不同平台（GitHub/Linear/Slack/Discord）的 webhook 归一化为 `UnifiedEvent` |
| **DeepAgent** | LangChain 的 stateful agent 框架（非 ReAct loop）；所有 workflow 跑其上 |
| **Middleware stack** | LangGraph hook 链；实现 budget / cancel / rate / circuit breaker 等横切关注点 |
| **Daytona sandbox** | Daytona 提供的 per-task Linux sandbox；fix workflow 在其中 clone repo / 跑测试 / patch |
| **Thread metadata** | LangGraph store 中每个 conversation thread 的状态快照（sandbox ID、加密 token、findings、queue） |
| **LiteLLM** | 多 vendor LLM 抽象；fallback Anthropic ↔ OpenAI ↔ Gemini |
| **per_task budget** | 单次任务（一个 issue/PR/fix/chat）的 cost 硬上限 |
| **fail-safe** | 安全防护词；指 bot 在不确定时选择"不行动 + 报告"而非"尝试 + 可能错" |
| **Shadow set** | 自建 PR 评测集，30-50 个真实 merged PR，跑 bot 看与人类 review 的一致性 |
| **SWE-bench Verified / Lite / Pro** | Princeton 系列代码修复 benchmark；Lite 入门（300），Verified 主流公开（500），Pro 防训练污染（1865，含 private） |

---

## 15. References

**内部演进文档**

- [Eval PRD（§8 完整 spec）](./openbot-eval-prd.md)
- [v0.1 PRD](../research/openbot-prd-v0.1.md) · [v0.2 PRD](../research/openbot-prd-v0.2.md) · [v0.3 PRD](../research/openbot-prd-v0.3.md)
- [80 问拷问清单](../research/openbot-interrogation.md)

**内部调研**

- [GitHub Bot 调研：robobun & similar](../research/robobun-and-similar-bots.md)
- [GitHub Bot 评测 benchmark 调研](../research/github-bot-evaluation-benchmarks.md)
- [Eval setup 推荐](../research/eval-setup-recommendation.md)
- [CI/CD & Evals 调研](../research/CICD_AND_EVALS_CN.md) · [Testing Design](../research/TESTING_DESIGN_CN.md) · [Project Guide / 架构总览](../research/PROJECT_GUIDE_CN.md)

**外部依赖**

- [LangChain DeepAgents](https://github.com/langchain-ai/deepagents) · [LiteLLM](https://github.com/BerriAI/litellm) · [Daytona](https://www.daytona.io/) · [Modal](https://modal.com/)
- [GitHub Apps docs](https://docs.github.com/en/apps) · [Trufflehog](https://github.com/trufflesecurity/trufflehog)
- [LangSmith](https://smith.langchain.com/) · [Langfuse](https://langfuse.com/) · [Inspect AI](https://inspect.ai-safety-institute.org.uk/)
- [SWE-bench](https://www.swebench.com/) · [Martian Code Review Benchmark](https://github.com/withmartian/CodeReviewBench) · [Aider Polyglot](https://aider.chat/docs/leaderboards/)
- [Cloudflare R2](https://www.cloudflare.com/products/r2/) · [Cursor BugBot resolution-rate methodology](https://cursor.com/blog/building-bugbot)

---

## Appendix · 给 contributor 的话

这份 PRD 的职责是约束当前 v0.1 alpha 闭环，而不是保存所有历史想法。仓库名、LLM 路由、sandbox backend、cancel label、预算默认值、评估 benchmark、CI 工作流等会漂移的决策以 §13 为准。

但 PRD 不能替代代码 in the wild 的发现。真正难的是：(1) Daytona sandbox 跑陌生 repo 的奇怪环境；(2) Trufflehog 在长 trace 输出里的 false positive；(3) 当前默认模型 vs release 推荐模型在不同语言下 review/fix 风格的细微差异；(4) GitHub App 每用户自建带来的 onboarding 长尾。

这些只能边写代码、边 dogfood、边收 issue 才能浮出来。文档更新的标准是：当代码、部署或 eval 状态变化时，及时更新当前状态和 cutline；不要把 v0.2+ 设想混回 v0.1 acceptance。

接下来优先级：`TaskSpec v3 queue contract` / `worker sandbox factory` / `stale tests cleanup` / `review output scan` / `chat read-only tools` / `triage label+priority`。LinearAdapter、plugin、dedup、docs site 都在 v0.2。

— 开干。
