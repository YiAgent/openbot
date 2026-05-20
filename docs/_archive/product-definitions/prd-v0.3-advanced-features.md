# OpenBot PRD v0.3 — MVP Final

> 起草日期：2026-05-14
> 状态：**v0.3 - 可执行的 MVP PRD**（v0.2 之后接受 4 个 push-back + v0.1 拆出 GitHub-only）
> 历史版本：[v0.1](./openbot-prd-v0.1.md) · [v0.2](./openbot-prd-v0.2.md)
> 配套：[80 问拷问清单](./openbot-interrogation.md) · [前置调研](../research/)

---

## v0.2 → v0.3 关键变更（5 项）

| # | 项 | v0.2 | **v0.3** |
|---|---|---|---|
| Δ1 | **MVP 切片** | GitHub + Linear 一起发（10-14 周） | **v0.1 GitHub-only（4-6 周）→ v0.2 + Linear + 社区 plugin PR（再 4-6 周）** |
| Δ2 | **Cost 限制** | 不限制 | **Soft cap + hard kill 三层硬限**（per-task / monthly / global） |
| Δ3 | **Rate 限制** | chat 不限速 | **每用户每日 / 每 repo 每小时 / 单 task cost cap 三层** |
| Δ4 | **取消机制** | 无 | **Label-based cancel**（加 `cancel-openbot` label → next step 自查）+ 全局 env kill switch |
| Δ5 | **Plugin 社区** | v0.3 才开放 | **v0.2 起允许社区 PR 内置 plugin（in-tree）**，v0.3 才开放 PyPI 沙箱 plugin |

**Roadmap 重新切片**：

```
v0.1 (4-6 周)    GitHub-only, 4 features
                 包含：cost cap / rate limit / cancel / 多语言复现 / Trufflehog
                 不含：Linear, 社区 plugin, dedup
                 → 可发 alpha 给社区试用

v0.2 (再 4-6 周) + Linear adapter
                 + 社区 in-tree plugin PR（不上 PyPI）
                 + Issue dedup
                 + 自家 audit log CLI
                 → MVP completed

v0.3 (再 2-3 月) + Slack / Discord
                 + Next.js Web frontend
                 + PyPI plugin + sandbox
                 + 多租户托管（可选）
```

---

## 0. 已锁定的 15 项根基决策

| # | 维度 | v0.3 决策 |
|---|---|---|
| 1 | 定位 | OSS 项目，汇集 community |
| 2 | 目标用户 | 个人 OSS maintainer（1-2 repo） |
| 3 | 差异化 | 开源 + 可插拔 + 多 channel 一体化 + 自带 API key |
| 4 | License | Apache-2.0 |
| 5 | Bot 身份 | GitHub App，**每用户自建** |
| 6 | 租户 | 单租户 self-host |
| 7 | 多 channel | **v0.1 仅 GitHub**；ChannelAdapter day-1 抽象；v0.2 加 Linear |
| 8 | MVP 功能 | 四个全上：triage + review + issue→PR + @mention chat |
| 9 | 插件 | LangGraph tool；**v0.2 起允许社区 PR in-tree plugin**；v0.3 开 PyPI |
| 10 | LLM | LiteLLM 多 vendor，默认 Claude |
| 11 | Entry 架构 | ChannelAdapter ABC（GitHubAdapter v0.1；LinearAdapter v0.2） |
| 12 | 触发 | review/triage 自动；fix 用 issue assign；chat 用 @mention 自然语言 |
| 13 | 配置 | `.openbot/config.yaml` |
| 14 | Sandbox | Modal |
| 15 | **限制** | **三层 cost cap + 三层 rate limit + label cancel + global kill switch** |

---

## 1. Executive Summary

OpenBot 是一个**开源、自托管、用户自带 LLM API key 的 GitHub 维护机器人**（v0.2 起多 channel）。每个用户自建 GitHub App、docker compose up 一个完全属于自己的实例。

**v0.1 MVP 4 个功能**（GitHub 上）：

1. 新 issue → 自动 **triage**：label + 优先级 + 尝试复现（Python/JS/TS）
2. 新 PR → 自动 **review**：摘要 + inline，severity threshold 过滤
3. Issue assign 给 bot → **fix agent loop**：写 patch → push 到主 repo → 开 PR → 跑 CI → self-fix
4. `@openbot <任何自然语言>` → **chat agent**：读代码、答问、解释（无保留词）

**核心安全/省钱机制（v0.3 新增）**：

- 三层 cost cap（per-task + monthly soft cap + global hard kill）
- 三层 rate limit（per-user/day + per-repo/hour + per-task cost cap）
- Label-based cancel（加 `cancel-openbot` label）
- Trufflehog 输出扫描 + sandbox env 隔离 + fork PR 默认不跑

**差异化**：开源 / 自托管 / 自带 API key / Plugin 系统 / 多 vendor LLM / 多 channel 架构预留。

**v0.1 不做**：Linear、社区 plugin、dedup、Web frontend、自动 merge、多租户。

---

## 2. v0.1 MVP 范围（GitHub-only）

### Scope 收紧到极致——只 GitHub，4 个功能

| 功能 | v0.1 | v0.2 | v0.3 |
|---|---|---|---|
| Triage（label / priority / 复现） | ✅ GitHub | + Linear | — |
| Review（PR） | ✅ GitHub | — | — |
| Issue → PR fix | ✅ GitHub | + Linear→GH | — |
| @mention chat | ✅ GitHub | + Linear | + Slack/Discord |
| Issue dedup | ❌ | ✅ | — |
| 社区 plugin（in-tree PR） | ❌ | ✅ | + PyPI |
| Web frontend | ❌ | ❌ | ✅ Next.js |
| Audit log CLI | ❌ | ✅ | + Web UI |

### v0.1 完成判据（4-6 周内）

- 4 个 workflow 在 dogfood repo（你自己的某个 repo）跑 7 天无 P1 bug
- SWE-bench Lite pass@1 ≥ 50%（Sonnet 4.5）
- Martian Code Review F1 ≥ 0.35
- 平均 fix 任务成本 ≤ $2.00
- 平均 review 评论数 / PR：1-4
- README + 3 篇核心文档：install / config / 4 features
- 1 个外部 OSS repo 试装跑 3 天

---

## 3. v0.3 新增：限制机制详解

### 3.1 三层 Cost Cap

```yaml
# .openbot/config.yaml
budget:
  # 第一层：单 task 硬上限（每次 LLM 调用累计）
  per_task:
    triage: 0.20         # 每个 issue 最多 $0.20
    review: 0.50         # 每个 PR 最多 $0.50
    fix:    3.00         # 每个 fix 任务最多 $3.00
    chat:   0.30         # 每个 @mention 最多 $0.30

  # 第二层：每 repo 每月软上限（超了暂停 + alert）
  monthly_soft_cap_usd: 100
  monthly_alert_at_pct: 80     # 用了 80% 通知 admin

  # 第三层：全局硬上限（超了完全停 + 必须人工 reset）
  global_hard_kill_usd: 500    # 整个 OpenBot 实例月度上限
```

**实现细节**：

- 每次 LiteLLM call 完成后，把 cost 写入 Postgres `cost_meter` 表
- agent middleware `BudgetEnforcement` 在每 step 之前检查累计 cost
- 超 per_task → agent 优雅停止 + comment "Hit per-task budget"
- 超 monthly_soft_cap → 本月剩余时间该 repo 的所有 workflow 跳过 + admin email
- 超 global_hard_kill → **整个 worker 池停 dequeue**，需 admin 跑 `openbot budget reset` 才恢复

**新 env var**：

```bash
OPENBOT_GLOBAL_HARD_KILL_USD=500    # 优先级高于 config，env 强制
```

### 3.2 三层 Rate Limit

```yaml
chat:
  rate_limit:
    per_user_per_day: 20         # 每用户每天最多 20 次 @mention
    per_repo_per_hour: 100       # 全 repo 每小时最多 100 次
    cost_cap_per_task: 0.30      # 单次 chat 上限（同 budget.per_task.chat）
```

**实现细节**：

- Redis 计数器，key 形如：
  - `rl:user:{user_id}:{YYYY-MM-DD}` → daily count
  - `rl:repo:{repo_id}:{YYYY-MM-DD-HH}` → hourly count
- 超限 → bot 评论 "Rate limited: 20/20 daily uses reached. Resets at 00:00 UTC."
- 不计 collaborator / owner（可配置：`rate_limit.exempt_roles: [owner, collaborator]`）

**默认值是建议起点**，OSS maintainer 可在 config 调高。

### 3.3 取消机制

**3 种方式**：

1. **Label-based cancel（主推）**：在被处理的 issue/PR 上加 `cancel-openbot` 标签
   - agent middleware 每 step 前 query GitHub API 看当前 issue label
   - 看到 `cancel-openbot` → 优雅停止 + 评论 "Cancelled by label"
   - 适合：fix 跑长了想停

2. **Chat comment cancel**：在 PR/issue 评论里 `@openbot stop` / `@openbot cancel` / `@openbot 停`
   - chat agent 识别 cancel intent（用 LLM judge）
   - 用 Redis pub/sub 信号给对应 worker，worker 下一 step 优雅退出
   - 适合：chat task 跑长了

3. **Global kill switch**（你已选）：env var `OPENBOT_KILL_SWITCH=true`
   - 所有 worker 立即停止 dequeue
   - 已在跑的 task 在下个 step 退出
   - 适合：紧急停整个 bot

**实现：agent middleware 模式**

```python
# openbot.application.middleware/cancellation.py
async def check_cancellation_before_step(state, runtime):
    if os.environ.get("OPENBOT_KILL_SWITCH") == "true":
        return STOP_WITH_MESSAGE("Global kill switch engaged")

    if await redis.sismember(f"cancel:thread:{state.thread_id}", "1"):
        return STOP_WITH_MESSAGE("Cancelled by user")

    # Check GitHub label every 30s（不是每 step，太贵）
    if state.step_num % 5 == 0:  # 每 5 step 查一次
        labels = await gh_api.get_issue_labels(state.repo_id, state.issue_num)
        if "cancel-openbot" in labels:
            return STOP_WITH_MESSAGE("Cancelled by label")

    return CONTINUE
```

### 3.4 Plugin 社区贡献流程（v0.2 起）

v0.2 起允许社区往 `openbot_plugins/` 目录提 PR 加新 plugin。

**贡献流程**：

1. Fork OpenBot
2. 在 `openbot_plugins/` 加新文件 `my_plugin.py`：

```python
# openbot_plugins/find_security_issues.py
from langchain_core.tools import tool

@tool
def find_security_issues(file_path: str) -> list[dict]:
    """Scan a file for common security issues (XSS, SQL injection, etc.)."""
    # ... implementation ...
    return [{"line": 42, "issue": "potential SQL injection", ...}]

TOOLS = [find_security_issues]
```

3. 加单元测试 `tests/plugins/test_find_security_issues.py`
4. 加文档 `docs/plugins/find_security_issues.md`
5. PR

**Plugin PR review checklist**（在 `CONTRIBUTING.md` 写明）：

- [ ] 没有外部网络调用（除 web_fetch 白名单）
- [ ] 不读 `os.environ`（防泄漏 secret）
- [ ] 不写文件到 sandbox 外
- [ ] 单元测试覆盖率 ≥ 80%
- [ ] tool docstring 完整（LLM 靠它调用）

**v0.2 plugin 仍跑主进程**，trust model = 主仓库 maintainer 信任。v0.3 plugin 跑独立 sandbox。

---

## 4. 功能规范（v0.1 锁定）

### 4.1 Triage

**触发**：自动（issue.opened）+ 可关
**Channel**：GitHub only（v0.2 加 Linear）
**Pipeline**：

1. **Auto-label**：从 config + repo 现有 label 中选 1-3 个
2. **Reproduce**（LLM 预判能否复现 → Modal sandbox 跑）
3. **Priority**：直接打 priority label（P0-P3）

**Budget**：默认 per_task $0.20

**Cancel**：加 `cancel-openbot` 立即停

**配置**：

```yaml
triage:
  enabled: true
  labels:
    available: [bug, enhancement, question, performance, docs, security]
    auto_discover_from_repo: true
  priority:
    enabled: true
    auto_apply: true
    labels: [priority/P0, priority/P1, priority/P2, priority/P3]
  reproduce:
    enabled: true
    languages: [python, javascript, typescript]
    sandbox:
      timeout_seconds: 180
      cpu: 2
      memory_mb: 4096
      allowed_network: [pypi.org, registry.npmjs.org, github.com]
```

详见 v0.2 §5.1。

### 4.2 PR Review

**触发**：自动（pull_request.opened + .synchronize）+ 默认对所有 PR 开
**Channel**：GitHub only（永久，Linear 没有 PR）
**Pipeline**：

1. 拉 PR diff（不拉完整文件）
2. LLM review 输出结构化 issue 列表
3. **按 severity threshold 过滤**（默认 `medium`，丢 low/nit）
4. 摘要评论 + inline 评论

**Budget**：默认 per_task $0.50

**Cancel**：加 `cancel-openbot`（在 PR 上）立即停

**配置**：

```yaml
review:
  enabled: true
  severity_threshold: medium
  skip_paths: ["tests/", "*.lock", "node_modules/"]
  blocking: false
  multi_turn: true
```

详见 v0.2 §5.2。

### 4.3 Issue → PR Fix

**触发**：assign issue 给 bot（仅 collaborator / write+ 能 assign）
**Channel**：GitHub only（v0.2 加 Linear→GitHub）
**Agent loop**：

1. ACK 评论 + LangSmith trace URL
2. Modal sandbox + clone repo
3. DeepAgent loop（read → write patch → run test → self-fix）
4. push 分支 `openbot/<issue-num>-<slug>` 到主 repo
5. 开 PR `draft=false`（你的选择）
6. CI 失败 → self-fix 最多 3 次
7. 永远不 merge

**Budget**：默认 per_task $3.00（最高的）

**Cancel**：
- 加 `cancel-openbot` label 在原 issue 或新开 PR 上
- 评论 `@openbot stop`
- Global kill

**Limits**：

```yaml
fix:
  enabled: true
  allowed_actors: [collaborator, owner]
  limits:
    max_steps: 80
    max_wall_seconds: 2700
    max_cost_usd: 3.00
    max_self_fix_attempts: 3
```

详见 v0.2 §5.3。

### 4.4 @mention Chat

**触发**：`@openbot <任何自然语言>`，任何人可触发
**Channel**：GitHub only（v0.2 加 Linear）
**Pipeline**：纯 LangGraph agent，工具白名单（read-only）

**Budget**：default per_task $0.30

**Rate limit**：
- 每用户每日 20 次
- 每 repo 每小时 100 次
- collaborator/owner 默认 exempt

**Cancel**：评论 `@openbot stop`

**配置**：

```yaml
chat:
  enabled: true
  allow_anyone: true
  rate_limit:
    per_user_per_day: 20
    per_repo_per_hour: 100
    cost_cap_per_task: 0.30
    exempt_roles: [owner, collaborator]
  allowed_tools:
    - read_file
    - glob
    - grep
    - shell_readonly
    - web_fetch
    - search_linked_issues
    - search_linked_prs
  forbidden_tools:
    - write_file
    - shell_write
    - gh_pr_create
    - gh_pr_merge
```

---

## 5. 完整配置示例 `.openbot/config.yaml`

```yaml
# OpenBot v0.3 config
version: 3

features:
  triage: true
  review: true
  fix: true
  chat: true

channels:
  github:
    enabled: true
  linear:                    # v0.2 启用
    enabled: false

model:
  primary: anthropic/claude-sonnet-4-5
  fallback: openai/gpt-5-mini
  per_feature:
    review: anthropic/claude-opus-4-7
    fix: anthropic/claude-opus-4-7

# ─────── v0.3 新增：限制机制 ───────
budget:
  per_task:
    triage: 0.20
    review: 0.50
    fix: 3.00
    chat: 0.30
  monthly_soft_cap_usd: 100
  monthly_alert_at_pct: 80
  global_hard_kill_usd: 500   # 也可用 env OPENBOT_GLOBAL_HARD_KILL_USD

# ─────── Triage ───────
triage:
  enabled: true
  labels:
    available: [bug, enhancement, question, performance, docs, security]
    auto_discover_from_repo: true
  priority:
    enabled: true
    auto_apply: true
    labels: [priority/P0, priority/P1, priority/P2, priority/P3]
  reproduce:
    enabled: true
    languages: [python, javascript, typescript]
    sandbox:
      timeout_seconds: 180
      cpu: 2
      memory_mb: 4096
      allowed_network: [pypi.org, registry.npmjs.org, github.com]

# ─────── Review ───────
review:
  enabled: true
  severity_threshold: medium
  skip_paths: ["tests/", "*.lock", "node_modules/", "vendor/"]
  blocking: false
  multi_turn: true

# ─────── Fix ───────
fix:
  enabled: true
  allowed_actors: [collaborator, owner]
  limits:
    max_steps: 80
    max_wall_seconds: 2700
    max_cost_usd: 3.00
    max_self_fix_attempts: 3
  branch:
    prefix: openbot/
    naming: "{issue_num}-{slug}"
  pr:
    draft: false
    auto_merge: false
  ci:
    wait_for_completion: true
    self_fix_on_failure: true

# ─────── Chat ───────
chat:
  enabled: true
  allow_anyone: true
  rate_limit:
    per_user_per_day: 20
    per_repo_per_hour: 100
    cost_cap_per_task: 0.30
    exempt_roles: [owner, collaborator]
  allowed_tools:
    - read_file
    - glob
    - grep
    - shell_readonly
    - web_fetch
    - search_linked_issues
    - search_linked_prs

# ─────── Cancel ───────
cancel:
  label: cancel-openbot           # 加此 label 即取消
  check_every_n_steps: 5          # agent 每 5 step 查一次（API 成本）
  comment_keywords: [stop, cancel, "停", "取消"]   # chat 识别 cancel intent

# ─────── Plugins (MVP 仅本地; v0.2 起允许社区 PR 加入 openbot_plugins/) ───────
plugins:
  local_dir: ./openbot_plugins/

# ─────── Security ───────
security:
  fork_pr: { run: false }
  secret_scan: { enabled: true, tool: trufflehog }
  audit_log: { enabled: true }
  kill_switch_env: OPENBOT_KILL_SWITCH

# ─────── Observability ───────
observability:
  langsmith:
    enabled: true
    project: openbot-prod
```

---

## 6. 架构（v0.1 简化版）

v0.1 阶段架构相比 v0.2 简化（无 Linear adapter）：

```
        ┌──────────────┐
        │  GitHub App  │
        │  (user-built)│
        └──────┬───────┘
               │ webhook
               ▼
┌──────────────────────────────────┐
│  Ingress (FastAPI)               │
│   - verify_webhook               │
│   - dedup via delivery_id        │
│   - return 202                   │
│   - enqueue → Redis              │
└────────────┬─────────────────────┘
             ▼
┌──────────────────────────────────┐
│  ChannelAdapter (ABC)            │
│   - GitHubAdapter (v0.1 only)    │
│   - LinearAdapter (v0.2)         │  ← stub in v0.1
└────────────┬─────────────────────┘
             ▼ UnifiedEvent
┌──────────────────────────────────┐
│  Router + Rate/Budget Check      │
│   - read .openbot/config.yaml    │
│   - check budget (per_task)      │
│   - check rate_limit             │
│   - check kill_switch            │
│   - dispatch workflow            │
└────────────┬─────────────────────┘
             ▼
   ┌──────────┼───────────┬─────────┐
   ▼          ▼           ▼         ▼
Triage    Review       Fix       Chat
Workflow  Workflow   Workflow  Workflow
   └──────────┴────┬──────┴─────────┘
                   ▼
         ┌──────────────────────┐
         │  LangGraph DeepAgent │
         │  + middleware:       │
         │    - ToolError       │
         │    - BudgetEnforce   │
         │    - CancelCheck     │
         │    - MsgQueue        │
         └──────────┬───────────┘
                    ▼
         ┌──────────────────────┐
         │  Modal Sandbox       │
         │  (per-thread)        │
         └──────────┬───────────┘
                    ▼
   ┌────────────────────────────────┐
   │  Storage                       │
   │   - Postgres (audit, cost,     │
   │     thread_metadata)           │
   │   - Redis (queue, rate, dedup) │
   │   - R2 (artifact archive)      │
   └────────────────────────────────┘
   ┌────────────────────────────────┐
   │  LangSmith (trace + cost)      │
   └────────────────────────────────┘
```

v0.2 加 LinearAdapter 时不需要重构这个图——只是 ChannelAdapter 多一个实现。

详见 v0.2 §7.2-§7.8（unchanged）。

---

## 7. 部署（v0.1 极简）

```bash
git clone https://github.com/yiwang/openbot && cd openbot
./setup.sh
# 1. 浏览器创建 GitHub App
# 2. 填 .env（App ID, private key, webhook secret, Anthropic key, Modal token, LangSmith key）
# 3. docker compose up -d
# 4. 浏览器去 GitHub 装 App 到 repo
# 5. 写 .openbot/config.yaml
```

预计 30 分钟。详细 wizard UX 在 v0.2 §10.2，v0.1 保持一致。

---

## 8. Roadmap（v0.3 重新切片）

### v0.1 - GitHub-only MVP（**4-6 周**）

**Week 1-2 · 框架基础**
- 把 Open SWE 当代码起点，重组为 `openbot/` 仓库
- LiteLLM 接入 + LangSmith trace
- Postgres + Redis + R2 docker-compose
- GitHub App `setup.sh` wizard

**Week 2-3 · ChannelAdapter + GitHubAdapter**
- ABC 定义
- GitHubAdapter 全实现（webhook verify / parse / reply / label / get_role）

**Week 3-4 · 4 个 Workflow**
- Triage workflow（label + priority + reproduce）
- Review workflow（diff + severity threshold + 摘要+inline）
- Fix workflow（assign trigger + agent loop + push to main repo）
- Chat workflow（natural language + tool whitelist）

**Week 4-5 · 限制 / 安全**
- BudgetEnforcement middleware
- CancelCheck middleware（label + comment + env）
- Rate limiter（Redis 计数）
- Trufflehog 输出扫描
- Fork PR 隔离

**Week 5-6 · Polish & Release**
- 多语言复现 sandbox（Python + JS/TS）
- README + 3 篇核心文档（install / config / 4 features）
- 自家 dogfood 7 天
- 1 个外部 OSS repo 试装
- 🎉 v0.1 alpha release

**v0.1 完成判据**：
- 4 workflow dogfood 7 天无 P1 bug
- SWE-bench Lite pass@1 ≥ 50%（Sonnet 4.5）
- Martian Code Review F1 ≥ 0.35
- 平均 fix 任务成本 ≤ $2.00
- 单 task budget 卡住率 < 5%（不能太敏感）
- Bot 评论 :+1: : :-1: 比例 ≥ 2:1

### v0.2 - 第二轮（**再 4-6 周**）

- **LinearAdapter** 完整实现 + 双 channel 跑通 fix
- **社区 in-tree plugin PR 流程**：CONTRIBUTING.md、PR template、3 个示例 plugin
- **Issue dedup**（embedding via Voyage + LLM rerank + pgvector）
- **`openbot audit` CLI**
- **DOcs site** 上 GitHub Pages

**v0.2 完成判据**：
- Linear 跑通 triage + chat + fix
- 接受 1-3 个外部 plugin PR
- Dedup Recall@10 ≥ 0.65
- 50+ install
- 100+ stars

### v0.3+ - 长期（**再 2-3 个月**）

- Slack adapter
- Discord adapter
- Next.js Web frontend（dashboard、audit log、budget viewer）
- PyPI plugin + plugin sandbox
- 多租户模式（可选托管）

### v1.0 目标

- 100+ stars, 50+ active install
- 3+ external community contributors
- 完整 docs site
- Plugin marketplace v1

---

## 9. 安全 & 滥用防护（v0.3 更新）

### 9.1 三层 Cost Cap（§3.1）✅
### 9.2 三层 Rate Limit（§3.2）✅
### 9.3 取消机制（§3.3）✅
### 9.4 Prompt Injection 防护（v0.2 §9.1 unchanged）
### 9.5 Fork PR 默认不跑（v0.2 §9.2 unchanged）
### 9.6 Config 改动需 admin 审批（v0.2 §9.3 unchanged）
### 9.7 审计 log（v0.2 §9.4 unchanged）
### 9.8 Trufflehog 输出扫 + sandbox env 隔离 + GitHub App 最小权限（v0.2 §9.7 unchanged）

---

## 10. Open Questions（剩下需要拍板）

| # | 项 | 我的建议 | 你拍 |
|---|---|---|---|
| OQ-1 | 项目仓库名 | `openbot` | _____ |
| OQ-2 | 默认 LLM 路由 | review/fix 用 Opus 4.7，triage/chat 用 Sonnet 4.5 | _____ |
| OQ-3 | R2 必须还是允许 local FS | 允许 local FS fallback（极小用户友好） | _____ |
| OQ-4 | rate limit 是否默认 exempt collaborator | **默认 exempt** | _____ |
| OQ-5 | cancel label 名 | `cancel-openbot` | _____ |
| OQ-6 | global hard kill 默认值 | $500（足够 100 个 fix task） | _____ |
| OQ-7 | monthly soft cap 默认值 | $100 per repo | _____ |
| OQ-8 | bot 评论英文 vs 跟随 issue 语言 | 跟随 LLM 自动判断 | _____ |
| OQ-9 | v0.1 内置 plugin（社区入门示例） | reproduce_python_issue / reproduce_js_issue / summarize_pr_diff | _____ |
| OQ-10 | docs 站 mkdocs-material vs Docusaurus | mkdocs-material（Python 友好） | _____ |
| OQ-11 | v0.1 是否 publish docker image 到 Docker Hub | 是（提升体验） | _____ |
| OQ-12 | 是否接受 plugin PR 必须含单测 | 是（CONTRIBUTING.md 强制） | _____ |

---

## 11. 成功指标（v0.3 更新）

### v0.1 alpha 目标（6 周内）

| Metric | Target |
|---|---|
| Dogfood 跑天数 | ≥ 7 |
| GitHub stars | ≥ 50 |
| 外部 install 数 | ≥ 5 |
| SWE-bench Lite pass@1 | ≥ 50% (Sonnet 4.5) |
| Martian Code Review F1 | ≥ 0.35 |
| 平均 fix 任务成本 | ≤ $2.00 |
| 单 task budget 卡住率 | < 5% |
| Bot 评论 :+1: : :-1: 比例 | ≥ 2:1 |

### v0.2 完整 MVP 目标（再 6 周后）

| Metric | Target |
|---|---|
| Install 数 | ≥ 50 |
| Stars | ≥ 100 |
| External plugin PR merged | ≥ 1 |
| Linear install | ≥ 5 |
| SWE-bench Verified pass@1 | ≥ 50% (Sonnet 4.5) |

---

## 12. Risks & Mitigations（v0.3 更新）

| Risk | Likelihood | Impact | v0.3 Mitigation |
|---|---|---|---|
| **LLM cost 失控** | ~~High~~ → Low | High | ✅ 三层 cost cap |
| **Chat 被滥用** | ~~High~~ → Low | Medium | ✅ 三层 rate limit |
| **bot 跑错任务无法停** | ~~High~~ → Low | Medium | ✅ Label-based cancel |
| **OSS community 不来** | Medium | Medium | ✅ v0.2 起 plugin PR + 详细 CONTRIBUTING.md |
| Prompt injection 漏 token | Medium | Critical | Trufflehog + 红队回归 |
| Bot 评论太 noisy | Medium | Medium | severity threshold + 👎 监控 |
| Modal 限流 / 故障 | Medium | High | abstraction 预留 fallback provider |
| Anthropic API 限流 | Medium | High | LiteLLM fallback to GPT |
| GitHub App 每用户自建 onboarding 痛 | High | Medium | 极其详细 setup wizard + 视频 |
| v0.1 6 周工期超 | Medium | Medium | 强收 scope；不行就砍 chat MVP，留 v0.2 |
| 与 OpenHands / Copilot 同质化 | High | Medium | 死守"OSS + 多 channel + plugin + 自带 key"卖点 |

---

## 13. References

- [v0.1 PRD](./openbot-prd-v0.1.md)
- [v0.2 PRD](./openbot-prd-v0.2.md)
- [80 问拷问清单](./openbot-interrogation.md)
- [robobun 调研](../research/robobun-and-similar-bots.md)
- [Benchmark 调研](../research/github-bot-evaluation-benchmarks.md)
- [Eval 推荐](../research/eval-setup-recommendation.md)
- [Open SWE CLAUDE.md](../../CLAUDE.md)
- [LangChain DeepAgents](https://github.com/langchain-ai/deepagents)
- [LiteLLM](https://github.com/BerriAI/litellm)
- [Modal](https://modal.com/)
- [GitHub Apps docs](https://docs.github.com/en/apps)
- [Trufflehog](https://github.com/trufflesecurity/trufflehog)
- [LangSmith](https://smith.langchain.com/)
- [Cloudflare R2](https://www.cloudflare.com/products/r2/)
- [Cursor BugBot resolution rate methodology](https://cursor.com/blog/building-bugbot)

---

## 14. v0.1 第一周可执行 task list（如果你想立刻开始）

| Day | Task | Owner |
|---|---|---|
| Day 1 | 创建 `openbot` repo，拷贝 Open SWE 骨架，重命名 module | Yi |
| Day 1-2 | LiteLLM 接入，写 `openbot.infrastructure.llm.model_router.py` | Yi |
| Day 2-3 | Postgres schema migration（cost_meter / audit_log / thread_metadata / rate_limit_counter） | Yi |
| Day 3-4 | ChannelAdapter ABC + GitHubAdapter 最小实现（verify + parse + reply） | Yi |
| Day 4-5 | GitHub App `setup.sh` interactive wizard | Yi |
| Day 5-6 | docker-compose.yml + `.env.example` + README install 段 | Yi |
| Day 6-7 | dogfood 跑 1 个 hello-world webhook（bot 收到 issue 自动评论 "hi"） | Yi |

第一周目标：bot 跑起来、能接 webhook、能回评论。功能逻辑 Week 2 开始。

---

## Appendix · 给你的诚实总结

到 v0.3，你的 PRD 已经从"宏大愿景"压到"可执行 MVP"。三个版本的演进：

| 版本 | 焦点 | 工期 | 状态 |
|---|---|---|---|
| v0.1 | 全面理想化 | 8-12 周 | 太大 |
| v0.2 | + Linear，但不限 cost/rate | 10-14 周 | 危险 |
| **v0.3** | **GitHub-only first + 完整限制** | **4-6 周 alpha** | ✅ 可执行 |

剩下要做的：

1. **拍 §10 的 12 个 Open Questions**——大部分一句话
2. **决定下周一起步还是先 review v0.3**
3. 想 (a) 我把 §14 第一周 task 拆成 GitHub Issue 模板 / (b) 我写 setup.sh 的实际代码 / (c) 我写 LinearAdapter 的 prototype（提前准备 v0.2）/ (d) 你自己开干，需要时再来找我

PRD 到这里已经够清晰可以动手了。再迭代意义不大——剩下的发现要 in code 里 surface，不是 in PRD 里 polish。
