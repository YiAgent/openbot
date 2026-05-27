# OpenBot v0.2 · Product Requirements Document

> 版本：**v0.2 Draft** · 起草日期：2026-05-27 · 状态：草案待审
> 前置版本：[v0.1 PRD](./openbot-prd.md) · [Eval PRD](./openbot-eval-prd.md)
> 目标：v0.1 alpha dogfood 通过后 6 周内交付 v0.2 完整 MVP

---

## 0. v0.1 现状审计

在规划 v0.2 之前，先诚实盘点 v0.1 的完成度。

### 0.1 v0.1 Alpha Readiness Checklist

| # | Gate (PRD §1.2) | 状态 | 说明 |
|---|---|---|---|
| 1 | `make check` 通过 | ✅ | 543 tests pass; 2 real_service/postgres 本地无 PG 时 expected fail |
| 2 | `make -C evals test` 通过 | ✅ | evals/ 目录干净，solvers 只通过 openbot.evaluation 调用 |
| 3 | 本地 signed webhook smoke | ⚠️ | E2E tests 存在 (test_webhook_e2e.py, test_triage_repro_e2e.py)，但未覆盖所有 6 种场景 |
| 4 | Webapp → worker payload contract 一致 | ✅ | TaskSpec v3 统一；decide_and_enqueue → worker routing 已端到端 |
| 5 | Fix sandbox-backed path | ✅ | Daytona sandbox 通过 SandboxPort 可插拔；runner._open_sandbox_if_configured() 统一生命周期 |
| 6 | Per-task / monthly / global budget gate 有测试 | ⚠️ | Monthly + global preflight 已有；per-task agent-loop cap 仍在 runtime 层未完成 |
| 7 | Bot output 不泄漏 synthetic secret | ✅ | Egress scanning via detect-secrets; egress boundary architecture test |
| 8 | README / PRD / deploy docs 不矛盾 | ⚠️ | 部分引用过时 (Daytona vs Modal, GLM vs Claude 默认值) |

### 0.2 功能完成度

| 功能 | 产品路径 | 评估面 | Alpha 剩余缺口 |
|---|---|---|---|
| **Triage** | ✅ Labels + priority + ACK + reproduce agent | ❌ 无 eval (等 triage 输出闭环) | 无阻塞项 |
| **Review** | ✅ Structured findings + severity filter + PR Review API + egress scan | ✅ review_martian | 无阻塞项 |
| **Fix** | ✅ Sandbox-backed + DeepAgents + branch + PR | ✅ fix_swe_bench | per-task budget 进 agent loop |
| **Chat** | ✅ Read-only tools + path allowlist + 8KB cap + state-change refusal | ✅ chat_swe_qa | 无阻塞项 |
| **Safety** | ✅ Kill switch + cancel + egress + budget + rate limit + fork PR gate | ✅ redteam_v0 | config approval gate |
| **Eval** | ✅ 4 surfaces via openbot.evaluation facade + LangSmith | ✅ | 无阻塞项 |
| **Infra** | ✅ TaskSpec v3 + hexagonal arch + 543 tests + Daytona sandbox | — | 无阻塞项 |

### 0.3 v0.1 遗留技术债

| 编号 | 债务 | 优先级 | 建议处置 |
|---|---|---|---|
| D1 | Per-task budget 未进 agent loop | High | v0.2 Sprint 1 修复 |
| D2 | Config approval gate 未实现 | Medium | v0.2 Sprint 2 |
| D3 | Docs 不一致 (Daytona vs Modal, GLM vs Claude) | Medium | v0.2 Sprint 1 随 docs site 一起修 |
| D4 | Webhook smoke 未覆盖全部 6 种场景 | Low | v0.2 补齐 E2E |
| D5 | Chat 无 shell_readonly / web_fetch | Low | v0.2 安全增强后考虑 |

---

## 1. v0.2 愿景

> "从 alpha dogfood 到可被外部 maintainer 真正使用的完整 MVP。"

v0.2 的核心目标：

1. **扩大 channel** — Linear adapter 让非 GitHub-first 的团队也能用
2. **开放插件** — 社区 in-tree plugin PR，建立 contributor 生态起步
3. **智能 dedup** — Issue 去重减少 maintainer 重复劳动
4. **运维可观测** — audit CLI 让 admin 能查、能导出、能 reset budget
5. **文档专业化** — mkdocs-material docs site，覆盖 install / config / 4 features / plugins
6. **评测升级** — Triage eval 接入、internal curated datasets、SWE-bench pass@1 ≥ 50%

---

## 2. v0.2 功能规格

### 2.1 LinearAdapter

**目标**：完整实现 `ChannelAdapter` 接口；Linear issue 触发 → fix workflow → 在 GitHub 开 PR → fix 完成回写 Linear comment。

**触发**：Linear webhook (`Issue.create`, `Issue.update`)

**数据流**：
```
Linear webhook
  → FastAPI /webhook/linear
  → LinearAdapter.parse_event() → UnifiedEvent
  → Router → preflight → fix workflow
  → fix 完成 → GitHub push + open PR
  → LinearAdapter.write_back() → Linear comment with PR link
```

**存储**：
- Linear OAuth token 加密存 Postgres `channel_credentials` 表
- `channel_credentials(user_id, channel, encrypted_token, created_at, expires_at)`

**配置**：
```yaml
channels:
  linear:
    enabled: true
    webhook_secret: ${OPENBOT_LINEAR_WEBHOOK_SECRET}
    oauth_client_id: ${OPENBOT_LINEAR_OAUTH_CLIENT_ID}
    oauth_client_secret: ${OPENBOT_LINEAR_OAUTH_CLIENT_SECRET}
```

**安全**：
- OAuth token AES-256-GCM 加密存储
- Webhook signature 验证（与 GitHub 同一模式）
- Rate limit 复用现有 Redis 计数器

**验收标准**：
1. Linear issue → fix workflow → GitHub PR 全链路 E2E test
2. Linear comment 回写包含 PR link
3. Channel credentials 加密存储 + 读取正确
4. `make check` 通过

---

### 2.2 社区 In-Tree Plugin PR

**目标**：允许社区往 `openbot_plugins/` 提 PR 加新 plugin，仍跑主进程。

**贡献流程**（写入 `CONTRIBUTING.md`）：
1. Fork → 在 `openbot_plugins/<name>.py` 实现 `@tool`
2. 单元测试 `tests/plugins/test_<name>.py`（**强制**，CI gate）
3. 文档 `docs/plugins/<name>.md`
4. 开 PR

**Plugin PR Review Checklist**：
- [ ] 没有外部网络调用（除 web_fetch 白名单）
- [ ] 不读 `os.environ`（防泄漏 secret）
- [ ] 不写文件到 sandbox 外
- [ ] 单元测试覆盖率 ≥ 80%（硬性要求）
- [ ] tool docstring 完整（LLM 靠 docstring 决定调用）

**内置示例 plugin**（v0.2 随 repo 发布）：
1. `reproduce_python_issue` — 已有，整理为 plugin 形式
2. `reproduce_js_issue` — JS/TS 版 reproduce
3. `summarize_pr_diff` — PR diff 摘要工具

**CI Gate**：
```yaml
# .github/workflows/plugin_pr.yml
- name: Plugin tests
  run: uv run pytest tests/plugins/ -v --cov=openbot_plugins --cov-fail-under=80
```

**验收标准**：
1. 3 个内置 plugin 各有单元测试 + 文档
2. CI 对 `openbot_plugins/` 变更强制跑 plugin test suite
3. `CONTRIBUTING.md` 包含完整贡献流程

---

### 2.3 Issue Dedup

**目标**：新 issue → 找出 top-3 候选 → 评论里给 maintainer 决策；**永不自动 close**。

**技术栈**：
- Embedding: Voyage-3-large（fallback: OpenAI text-embedding-3-large）
- 存储: pgvector extension on Postgres
- Rerank: Claude Sonnet (top-10 → 语义重复判断)

**Pipeline**：
```
issue.opened
  → embed title + body
  → pgvector ANN search (top-10 by cosine similarity)
  → LLM rerank → top-3 candidates
  → 评论: "This issue may be related to #X, #Y, #Z. Please check if this is a duplicate."
  → 维护者手动决策（永不自动 close）
```

**配置**：
```yaml
dedup:
  enabled: true
  embedding_provider: voyage  # or openai
  embedding_model: voyage-3-large
  top_k: 10
  rerank_model: anthropic/claude-sonnet-4-6
  min_similarity: 0.75
```

**数据库**：
```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE issue_embeddings (
    id SERIAL PRIMARY KEY,
    repo_id BIGINT NOT NULL,
    issue_number INT NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(repo_id, issue_number)
);
CREATE INDEX ON issue_embeddings USING ivfflat (embedding vector_cosine_ops);
```

**验收标准**：
1. 新 issue 自动查重并评论 top-3 候选
2. Dedup Recall@10 ≥ 0.65 (v0.2 目标)
3. 永不自动 close，只 propose
4. Embedding provider 可配置（voyage / openai）

---

### 2.4 `openbot audit` CLI

**目标**：运维可观测 — admin 能查、能导出、能 reset budget。

**命令**：
```bash
# 列出最近 N 天的 task 记录
openbot audit list --since=7d --feature=fix
openbot audit list --since=24h --repo=owner/repo

# 查看单个 task 详情
openbot audit show <task_id>

# 导出为 CSV
openbot audit export --format=csv --since=30d > audit.csv

# 重置 budget（紧急用）
openbot budget reset --repo=owner/repo
openbot budget reset --global
```

**数据源**：Postgres `audit_log` + `cost_meter` 表（v0.1 已有 schema）

**输出格式**：
- `list`: 表格 (task_id, feature, repo, cost_usd, outcome, created_at)
- `show`: 完整 JSON (含 trigger, actor, labels, outcome, cost breakdown)
- `export`: CSV / JSON

**验收标准**：
1. 4 个子命令 (list, show, export, budget reset) 各有单元测试
2. `openbot budget reset --global` 清零 global_hard_kill 计数器
3. CLI 通过 `openbot[cli]` entrypoint 可用

---

### 2.5 Docs Site

**目标**：mkdocs-material docs site，GitHub Pages 部署。

**技术选型**：mkdocs-material（锁定：相比 Docusaurus 更对齐 Python 生态）

**目录结构**：
```
docs/
├── index.md              # Landing page
├── install/
│   ├── quickstart.md     # 30 分钟 self-host 指南
│   ├── docker.md         # Docker Compose 部署
│   └── heroku.md         # Heroku 部署
├── config/
│   ├── reference.md      # .openbot/config.yaml 完整参考
│   └── examples.md       # 常见配置示例
├── features/
│   ├── triage.md
│   ├── review.md
│   ├── fix.md
│   └── chat.md
├── plugins/
│   ├── authoring.md      # Plugin 开发指南
│   └── builtin.md        # 内置 plugin 列表
├── eval/
│   ├── overview.md       # 评测体系概览
│   └── benchmarks.md     # Benchmark 套件说明
├── security/
│   ├── overview.md       # 安全模型
│   └── threat-model.md   # 威胁模型与缓解
└── faq.md
```

**CI**：
```yaml
# .github/workflows/docs.yml
- name: Deploy docs
  uses: mhausenbaum/mkdocs-material-github-pages@v3
  with:
    requirements: docs/requirements.txt
```

**验收标准**：
1. ≥ 15 篇文档覆盖 install / config / 4 features / plugins / eval / security / FAQ
2. GitHub Pages 自动部署
3. 搜索功能可用
4. 中英文双语（英文为主，中文为辅）

---

## 3. v0.1 遗留修复 (Sprint 1)

在开始 v0.2 新功能之前，先修复 v0.1 遗留技术债。

### 3.1 Per-Task Budget 进 Agent Loop

**问题**：当前 per-task budget 只在 preflight 检查（任务开始前），但 agent loop 执行过程中没有 step-level 检查。

**方案**：在 DeepAgents runtime middleware stack 中加入 `BudgetStepGuard`：
- 每 step 执行前查 `cost_meter` 累计
- 超 per_task cap → 优雅停止 + 评论 "Hit per-task budget ($3.00)"
- 与现有 `BudgetEnforcement` (input-side) 共享 `cost_meter` 查询逻辑

**文件**：`openbot/infrastructure/agents/_budget_middleware.py`

### 3.2 Config Approval Gate

**问题**：`.openbot/config.yaml` 的 PR 改 budget / allowed_tools 等高风险字段时，没有审批机制。

**方案**：
- Preflight 阶段检测 config 变更 PR
- 高风险字段 (budget.*, safety.*, channels.*) 变更 → 需 `config-approved` label
- 无 label → bot 评论 "Config change requires approval. Add `config-approved` label."
- Alpha 阶段先以 preflight check 落地，不做自动 merge

---

## 4. v0.2 评测升级

### 4.1 Triage Eval 接入

**前置条件**：v0.1 triage 输出 label + priority 已完成 → 可以接入 eval。

| Suite | Dataset | 指标 | 目标 |
|---|---|---|---|
| `triage_gitbugs` | GitBugs subset | macro_f1 | TBD (dataset 建立后定义) |
| `triage_internal_v1` | 历史 issue ≥ 200 | macro_f1, priority_accuracy | dataset 建立后定义 |

### 4.2 Internal Curated Datasets

| Suite | 用途 | 规模 | 标注方式 |
|---|---|---|---|
| `review_internal_v1` | Review 质量回归 | ≥ 200 PR | 三分类: useful / noise / wrong |
| `fix_internal_v1` | Fix 成功率 | ≥ 200 issue | pass@1 + unrelated_change_rate |
| `chat_internal_v1` | Chat 正确性 | ≥ 200 Q&A | 人工标注 correctness |

### 4.3 SWE-bench 目标

| 指标 | v0.1 baseline | v0.2 目标 |
|---|---|---|
| SWE-bench Verified pass@1 (Sonnet 4.6) | TBD (首次跑) | ≥ 50% |
| Martian review mean_f1 | TBD | ≥ 0.55 |
| SWT-bench (test generation) | unsupported=true | 产品能力实现后真实评分 |

### 4.4 Safety Eval 升级

| Suite | 规模 | 指标 | 目标 |
|---|---|---|---|
| `redteam_v0` | 24 prompts | mean_safe | 1.00 (保持) |
| `redteam_v1` | 100+ prompts | mean_safe | 1.00 |

新增攻击向量：间接注入、secret exfiltration、tool misuse。

---

## 5. Sprint 规划

### Sprint 1 (Week 1-2): 技术债修复 + 基础设施

| 任务 | 优先级 | 预估 |
|---|---|---|
| Per-task budget 进 agent loop | High | 2d |
| Config approval gate | Medium | 1d |
| Docs 不一致修复 | Medium | 1d |
| mkdocs-material docs site 搭建 | High | 3d |
| E2E smoke 覆盖补齐 | Low | 1d |

### Sprint 2 (Week 3-4): LinearAdapter + Plugin 系统

| 任务 | 优先级 | 预估 |
|---|---|---|
| LinearAdapter webhook + parse_event | High | 3d |
| LinearAdapter write_back + E2E | High | 2d |
| Plugin 框架 (openbot_plugins/) | High | 2d |
| 3 个内置 plugin | Medium | 3d |
| CONTRIBUTING.md + plugin CI gate | Medium | 1d |

### Sprint 3 (Week 5-6): Dedup + Audit + Eval 升级

| 任务 | 优先级 | 预估 |
|---|---|---|
| Issue dedup (embedding + pgvector + rerank) | High | 4d |
| `openbot audit` CLI | Medium | 2d |
| Triage eval 接入 | Medium | 1d |
| Internal curated dataset 搭建 | Medium | 2d |
| SWE-bench pass@1 baseline 跑通 | High | 2d |

---

## 6. 成功指标

### 6.1 Engineering

| 指标 | 目标 |
|---|---|
| `make check` | 通过 |
| Test suite | ≥ 600 tests |
| E2E smoke | 覆盖 triage / review / fix / chat / linear 5 种场景 |
| Per-task budget enforcement | 有 agent-loop step-level 测试 |
| Plugin CI gate | `openbot_plugins/` 变更强制跑 plugin tests |

### 6.2 Quality

| 指标 | 目标 |
|---|---|
| SWE-bench Verified pass@1 (Sonnet 4.6) | ≥ 50% |
| Martian review mean_f1 | ≥ 0.55 |
| Dedup Recall@10 | ≥ 0.65 |
| Redteam_v1 mean_safe | 1.00 |

### 6.3 Adoption

| 指标 | 目标 |
|---|---|
| Install 数 | ≥ 50 |
| Stars | ≥ 100 |
| External plugin PR merged | ≥ 1 |
| Linear install | ≥ 5 |
| Docs site pages | ≥ 15 |

---

## 7. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Linear API 变更 | Medium | Medium | 抽象 ChannelAdapter；Linear SDK 版本锁定 |
| pgvector 性能 | Low | Medium | IVFFlat 索引；top-K 限制 10 |
| Plugin 安全 | Medium | High | CI gate + review checklist + no os.environ |
| SWE-bench pass@1 < 50% | Medium | Medium | 持续 prompt engineering + model fallback |
| 6 周工期超 | Medium | Medium | 强收 scope；dedup 可延到 v0.2.1 |

---

## 8. 与 v0.1 PRD 的关系

v0.2 PRD 是 v0.1 PRD 的增量补充，不替代。以下内容继续以 v0.1 PRD 为准：

- §2 产品定位与差异化
- §3 目标用户
- §4.1-4.8 功能规格 (triage/review/fix/chat + safety)
- §5 架构 (v0.2 架构增量见本 PRD §2.1)
- §6 配置
- §7 部署
- §8 Quality & Evaluation (v0.2 增量见本 PRD §4)
- §13 关键决策

v0.2 新增决策：

| # | 决策项 | 值 | 理由 |
|---|---|---|---|
| 13 | Linear OAuth 存储 | AES-256-GCM 加密 Postgres | 与 GitHub token 同一模式 |
| 14 | Dedup embedding | Voyage-3-large (fallback: OpenAI) | 代码检索质量最佳 |
| 15 | Dedup rerank | Claude Sonnet | 语义判断准确率高 |
| 16 | Plugin trust model | 仓库 maintainer 信任 (in-tree PR) | v0.3 才上 PyPI 沙箱 |
| 17 | Docs framework | mkdocs-material | Python 生态友好 |

---

## 9. Glossary (v0.2 新增)

| 术语 | 含义 |
|---|---|
| **LinearAdapter** | ChannelAdapter 的 Linear 实现；webhook → UnifiedEvent → write-back |
| **pgvector** | Postgres 向量扩展；用于 issue embedding 近邻搜索 |
| **in-tree plugin** | 存放在 `openbot_plugins/` 目录下的工具，通过 PR 贡献，CI gate 保护 |
| **config-approved** | Label 名；`.openbot/config.yaml` 高风险字段变更需此 label 才生效 |

---

## Appendix · Sprint 1 详细任务清单

### T1: Per-Task Budget 进 Agent Loop

```
文件: openbot/infrastructure/agents/_budget_middleware.py
改动:
  - 新增 BudgetStepGuard middleware
  - 每 step 前查 cost_meter 累计 vs per_task_cap_usd
  - 超限 → raise BudgetExceeded → agent 优雅停止 + 评论
测试:
  - tests/unit/infrastructure/agents/test_budget_step_guard.py
  - 场景: 未超限继续 / 超限停止 / cost_meter 查询失败降级
```

### T2: Config Approval Gate

```
文件: openbot/application/middleware/config_approval.py
改动:
  - 检测 config 变更 PR (diff .openbot/config.yaml)
  - 高风险字段列表: budget.*, safety.*, channels.*, cancel.*
  - 无 config-approved label → bot 评论 + BLOCKED
测试:
  - tests/integration/middleware/test_config_approval.py
  - 场景: 普通 config 改 / 高风险字段改 / 有 approval label
```

### T3: Docs Site 搭建

```
文件: mkdocs.yml, docs/**/*.md
改动:
  - mkdocs-material 项目初始化
  - 15+ 篇文档
  - GitHub Pages CI workflow
测试:
  - CI: mkdocs build --strict (无 broken link)
```
