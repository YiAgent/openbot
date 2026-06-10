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
4. **上下文感知** — Code Graph + 语义检索，让 agent 看到 diff 之外的关键上下文
5. **运维可观测** — audit CLI 让 admin 能查、能导出、能 reset budget
6. **文档专业化** — mkdocs-material docs site，覆盖 install / config / 4 features / plugins
7. **评测升级** — Triage eval 接入、internal curated datasets、SWE-bench pass@1 ≥ 50%

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

### 2.6 Context Management Layer (Code Graph + 语义检索)

> **动机**：v0.1 的 agent 只能看到 prompt 中内联的 diff（截断 64KB）+ 运行时 tool call 搜索。没有预索引、没有语义搜索、没有跨文件依赖追踪。Review agent 经常遗漏与变更相关的关键上下文（测试文件、调用方、类型定义）。

**目标**：构建代码图（Code Graph）+ 语义检索层，让 agent 在执行前就能获取与当前变更最相关的上下文文件、历史 PR、团队规范。

#### 2.6.1 架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Context Management Layer                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │  Code Graph  │    │   Embedding  │    │  Context Retriever   │  │
│  │  Builder     │───▶│   Indexer    │───▶│  (query-time)        │  │
│  │  (tree-sitter)│    │  (LanceDB)   │    │                      │  │
│  └──────────────┘    └──────────────┘    └──────────────────────┘  │
│         │                    │                      │               │
│         ▼                    ▼                      ▼               │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │  AST chunks  │    │  Vector      │    │  Ranked context      │  │
│  │  + dep graph │    │  embeddings  │    │  → agent prompt      │  │
│  └──────────────┘    └──────────────┘    └──────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Multi-Source Aggregator                                      │  │
│  │  code chunks + issues + PRs + team conventions + linter rules │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

#### 2.6.2 Code Graph Builder (AST-aware 代码切片)

**技术选型**：tree-sitter（支持 40+ 语言，增量解析，生产级成熟）

**切片策略**（三级粒度）：

| 粒度 | 切片单位 | 用途 |
|------|---------|------|
| L1: File | 整个文件 | 模块概览、import 关系 |
| L2: Class/Function | 类 / 函数定义 | 精准上下文注入 |
| L3: Diff-hunk | AST diff 对应的结构化块 | Review 场景的核心切片 |

**Code Graph 数据结构**：

```python
@dataclass(frozen=True)
class CodeChunk:
    chunk_id: str                    # SHA256(repo + path + start_line + end_line)
    repo_id: str
    file_path: str
    language: str
    chunk_type: Literal["file", "class", "function", "method", "diff_hunk"]
    name: str                        # 函数/类名，文件级为文件名
    content: str                     # 源码
    start_line: int
    end_line: int
    parent_chunk_id: str | None      # 所属类/模块
    dependencies: frozenset[str]     # import / 调用的其他 chunk_id
    metadata: ChunkMetadata          # 语言、AST 节点类型、最后修改时间

@dataclass(frozen=True)
class ChunkMetadata:
    ast_node_type: str               # tree-sitter 节点类型 (function_definition, class_declaration, ...)
    docstring: str | None
    decorators: tuple[str, ...]      # Python decorators, Java annotations, etc.
    complexity: int                  # 圈复杂度 (粗估)
    last_commit_sha: str
    last_modified_at: datetime
```

**依赖图构建**：

```python
@dataclass(frozen=True)
class DependencyEdge:
    source_chunk_id: str
    target_chunk_id: str
    edge_type: Literal["imports", "calls", "inherits", "implements", "type_uses"]
    weight: float                    # 基于调用频率/引用次数
```

依赖图存储为有向图（NetworkX in-memory → 序列化到 LanceDB metadata），支持：
- **前向查询**：`userAuth()` 变更 → 找到所有调用方
- **后向查询**：`OrderService` 变更 → 找到它依赖的所有模块
- **传递闭包**：变更影响范围的完整图景

#### 2.6.3 Embedding Indexer (向量索引)

**技术选型**：LanceDB（嵌入式向量数据库，零拷贝读取，无需独立服务器）

**选型理由**（对比 pgvector / ChromaDB / Qdrant）：

| 维度 | LanceDB | pgvector | ChromaDB |
|------|---------|----------|----------|
| 部署 | 嵌入式（类 SQLite） | Postgres 扩展 | 独立服务 |
| 适用场景 | CLI / 单实例 | 已有 Postgres 的场景 | Python 快速原型 |
| 性能 | <1ms 100 万向量查询 | 依赖 PG 配置 | 中等 |
| 版本控制 | Git-like 内置版本 | 无 | 无 |
| 多模态 | 原生支持代码+元数据 | 需额外列 | 有限 |

**决策**：Issue dedup 继续使用 pgvector（已有 Postgres 基础设施，issue embedding 是简单的文本嵌入）；代码检索使用 LanceDB（需要更复杂的代码元数据、AST 结构、依赖图，且需要本地高性能查询）。

**LanceDB Schema**：

```python
# code_chunks 表
schema = {
    "chunk_id": str,                 # 主键
    "repo_id": str,
    "file_path": str,
    "language": str,
    "chunk_type": str,               # file / class / function / method / diff_hunk
    "name": str,
    "content": str,
    "embedding": vector(1024),       # Voyage-3-large 维度
    "start_line": int,
    "end_line": int,
    "parent_chunk_id": str | None,
    "dependencies": list[str],       # 依赖的 chunk_id 列表
    "ast_node_type": str,
    "complexity": int,
    "last_commit_sha": str,
    "last_modified_at": timestamp,
    "indexed_at": timestamp,
}

# code_dependencies 表 (依赖图边)
dep_schema = {
    "edge_id": str,
    "source_chunk_id": str,
    "target_chunk_id": str,
    "edge_type": str,                # imports / calls / inherits / implements / type_uses
    "weight": float,
}
```

**索引策略**：

```python
# 向量索引: IVF-PQ (磁盘高效，适合代码库规模)
table.create_index(
    metric="cosine",
    index_type="IVF_PQ",
    num_partitions=256,      # 根据 repo 规模调整
    num_sub_vectors=16,
)

# 标量索引: 文件路径 + 语言 (过滤查询)
table.create_scalar_index("file_path")
table.create_scalar_index("language")
table.create_scalar_index("repo_id")
```

**嵌入策略**：

| 内容类型 | 嵌入方式 | 说明 |
|---------|---------|------|
| 代码 chunk | Voyage-3-large | 代码检索质量最佳 |
| AST 路径 | code2vec-style path embedding | 结构信息补充 |
| Issue / PR 文本 | Voyage-3-large | 与 issue dedup 共享 |
| 团队规范 | Voyage-3-large | 自定义审查指令 |

混合检索 = 向量相似度 + 全文检索(BM25) + SQL 过滤（语言、文件路径、时间范围）

#### 2.6.4 Context Retriever (上下文检索器)

**检索流程**：

```
PR opened
  │
  ├─ 1. Diff Parser ──▶ 提取变更的文件 + 行范围
  │
  ├─ 2. AST Diff ──▶ 识别结构性变更（新增函数、修改接口、删除类）
  │
  ├─ 3. Chunk Mapper ──▶ 变更行 → CodeChunk 映射
  │
  ├─ 4. Dependency Walk ──▶ 沿依赖图扩展上下文
  │     ├── 调用方 (谁调用了变更的函数?)
  │     ├── 被调用方 (变更的函数调用了谁?)
  │     ├── 类型依赖 (变更的类型被谁使用?)
  │     └── 测试文件 (哪些测试覆盖了变更的代码?)
  │
  ├─ 5. Vector Search ──▶ 语义相似的代码片段
  │     ├── 相似实现 (其他地方有没有类似的模式?)
  │     ├── 相关文档 (哪些文档提到了变更的 API?)
  │     └── 历史变更 (类似变更的 PR 和 review 意见)
  │
  ├─ 6. Multi-Source Aggregator ──▶ 合并排序
  │     ├── 代码 chunks (权重: 0.5)
  │     ├── 相关 issues (权重: 0.2)
  │     ├── 历史 PRs (权重: 0.15)
  │     └── 团队规范/linter 规则 (权重: 0.15)
  │
  └─ 7. Token Budget Allocator ──▶ 注入 agent prompt
        ├── diff: 64KB (已有)
        ├── code context: 32KB (新增)
        ├── issue/PR context: 8KB (新增)
        └── team conventions: 4KB (新增)
```

**Token 预算分配**（总上下文窗口 200K tokens）：

| 内容 | 预算 | 优先级 |
|------|------|--------|
| PR diff | 64KB (~16K tokens) | P0: 始终包含 |
| 直接相关代码 (依赖图 1-hop) | 32KB (~8K tokens) | P0: 始终包含 |
| 语义相似代码 (向量检索 top-5) | 16KB (~4K tokens) | P1: 空间允许时包含 |
| 相关 issues / PRs | 8KB (~2K tokens) | P1: 空间允许时包含 |
| 团队规范 / linter 规则 | 4KB (~1K tokens) | P2: 最低优先级 |

当总上下文超出预算时，按优先级裁剪，同一优先级内按相关性分数排序。

#### 2.6.5 与 Agent Runtime 集成

**集成点**：`BaseDeepAgentRuntime` + 各 `Profile`

```python
class ContextInjectionMiddleware:
    """在 agent 执行前注入检索到的上下文"""

    def __init__(self, retriever: ContextRetriever):
        self.retriever = retriever

    async def __call__(self, request: AgentRequest, next_fn):
        # 1. 检索相关上下文
        context = await self.retriever.retrieve(
            repo_id=request.repo_id,
            diff=request.diff,
            issue_body=request.issue_body,
            budget=ContextBudget(
                code_context=32_000,
                issue_context=8_000,
                convention_context=4_000,
            ),
        )

        # 2. 注入到 request metadata
        request = request.with_context(context)

        # 3. 继续执行
        return await next_fn(request)
```

**Profile 变更**（以 ReviewProfile 为例）：

```python
# 现有: system_prompt 只包含 diff
# 新增: system_prompt 包含 diff + 相关代码 + 团队规范

def system_prompt(self, request: AgentRequest) -> str:
    sections = [self._base_prompt]

    if request.context.code_chunks:
        sections.append(self._format_code_context(request.context))

    if request.context.related_issues:
        sections.append(self._format_issue_context(request.context))

    if request.context.team_conventions:
        sections.append(self._format_conventions(request.context))

    return "\n\n".join(sections)
```

#### 2.6.6 Indexing Pipeline (索引构建)

**触发时机**：

| 事件 | 动作 | 增量/全量 |
|------|------|----------|
| `push` to default branch | 更新变更文件的 chunks | 增量 |
| `PR merged` | 更新变更文件的 chunks + 依赖图 | 增量 |
| `repo.install` (首次接入) | 全仓库索引 | 全量 |
| `repo.reindex` (手动触发) | 全仓库重建 | 全量 |

**增量索引流程**：

```
git diff HEAD~1 --name-only
  → tree-sitter 解析变更文件
  → 重新切片 → 生成 embeddings
  → LanceDB upsert (chunk_id 为主键)
  → 更新依赖图边 (删除旧边 + 插入新边)
```

**性能目标**：

| 指标 | 目标 |
|------|------|
| 全量索引速度 | ≥ 1000 files/min |
| 增量索引延迟 | < 30s (单文件) |
| 检索延迟 (P99) | < 500ms |
| 索引存储开销 | < 2x 源码大小 |

#### 2.6.7 回退策略

当 LanceDB 不可用或未部署时（如自托管用户不想装 LanceDB）：

1. **降级到 GitHub Code Search**：现有的 `grep_repo` tool 作为 fallback
2. **跳过语义检索**：只使用依赖图（基于 tree-sitter AST 分析，不需要向量数据库）
3. **最小模式**：只使用 diff 内联 + tool call（v0.1 行为）

```yaml
context:
  provider: lancedb      # lancedb | github_search | minimal
  lancedb_path: .openbot/lancedb  # 本地 LanceDB 路径
  embedding_provider: voyage      # voyage | openai
  embedding_model: voyage-3-large
  max_code_context_kb: 32
  max_issue_context_kb: 8
  enable_dependency_graph: true   # 即使不用向量检索，也构建依赖图
```

#### 2.6.8 验收标准

1. Review agent 能获取变更函数的调用方和测试文件（依赖图 1-hop）
2. 向量检索返回的代码片段与变更语义相关（人工评估 relevance ≥ 0.7）
3. 增量索引 < 30s 完成（单文件变更）
4. 全量索引支持 ≥ 10K 文件的仓库
5. LanceDB 不可用时自动降级到 GitHub Code Search
6. Context injection 不超出 token 预算（有测试验证）
7. `make check` 通过，新增 ≥ 50 个测试

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

### Sprint 3 (Week 5-6): Dedup + Context Layer + Audit + Eval 升级

| 任务 | 优先级 | 预估 |
|---|---|---|
| Issue dedup (embedding + pgvector + rerank) | High | 4d |
| Context Layer: tree-sitter code chunker + dependency graph | High | 3d |
| Context Layer: LanceDB indexer + embedding pipeline | High | 2d |
| Context Layer: ContextRetriever + agent runtime 集成 | High | 3d |
| `openbot audit` CLI | Medium | 2d |
| Triage eval 接入 | Medium | 1d |
| Internal curated dataset 搭建 | Medium | 2d |
| SWE-bench pass@1 baseline 跑通 | High | 2d |

> **注意**：Context Layer 是 Sprint 3 的关键路径。如果工期紧张，可先交付 tree-sitter 依赖图（不需要 LanceDB），向量检索延到 v0.2.1。

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
| Context retrieval relevance (人工评估) | ≥ 0.70 |
| Context retrieval P99 latency | < 500ms |
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
| LanceDB 索引膨胀 | Medium | Medium | 增量索引 + 过期 chunk 清理 + 压缩策略 |
| tree-sitter 语言覆盖不全 | Low | Low | 优先支持 Python/JS/TS/Go/Rust/Java；其他语言降级到文本切片 |
| Context 检索噪声 | Medium | Medium | 依赖图 1-hop 限制 + 相关性阈值过滤 + 人工评估 loop |
| Embedding 成本 | Medium | Low | 增量嵌入（只嵌入变更文件）+ 缓存 + Voyage 批量 API |
| 6 周工期超 | Medium | Medium | 强收 scope；Context Layer 可分阶段交付（依赖图优先，向量检索次之） |

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
| 18 | 代码向量数据库 | LanceDB（嵌入式） | 无需独立服务器，零拷贝，Git-like 版本控制 |
| 19 | 代码解析器 | tree-sitter | 40+ 语言支持，增量解析，生产级成熟 |
| 20 | 代码嵌入模型 | Voyage-3-large | 代码检索质量最佳，与 issue dedup 共享 |
| 21 | Context 降级策略 | LanceDB → GitHub Code Search → 最小模式 | 自托管用户可选降级，无需强制装 LanceDB |

---

## 9. Glossary (v0.2 新增)

| 术语 | 含义 |
|---|---|
| **LinearAdapter** | ChannelAdapter 的 Linear 实现；webhook → UnifiedEvent → write-back |
| **pgvector** | Postgres 向量扩展；用于 issue embedding 近邻搜索 |
| **in-tree plugin** | 存放在 `openbot_plugins/` 目录下的工具，通过 PR 贡献，CI gate 保护 |
| **config-approved** | Label 名；`.openbot/config.yaml` 高风险字段变更需此 label 才生效 |
| **Code Graph** | 代码依赖图；基于 AST 分析构建的函数/类/模块间调用、继承、导入关系图 |
| **LanceDB** | 嵌入式向量数据库；用于代码 chunk 的向量存储和语义检索 |
| **tree-sitter** | 增量解析器生成器；支持 40+ 语言的 AST 解析，用于代码结构化切片 |
| **Context Retriever** | 上下文检索器；根据 PR diff 检索相关代码、issues、PRs、团队规范，注入 agent prompt |
| **Code Chunk** | 代码切片；tree-sitter 按函数/类/文件边界切分的语义单元 |

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

---

## Appendix B · Sprint 3 Context Layer 详细任务清单

### T4: tree-sitter Code Chunker + Dependency Graph

```
依赖: tree-sitter (pip: tree-sitter, tree-sitter-python, tree-sitter-javascript, ...)
新增文件:
  openbot/infrastructure/context/
  ├── __init__.py
  ├── chunker.py          # CodeChunker: AST → CodeChunk 列表
  ├── dep_graph.py        # DependencyGraph: 构建 + 查询
  ├── models.py           # CodeChunk, ChunkMetadata, DependencyEdge dataclass
  └── languages/
      ├── __init__.py
      ├── python.py       # Python-specific AST 提取逻辑
      ├── javascript.py   # JS/TS-specific
      └── generic.py      # 通用 fallback (基于正则)

改动:
  pyproject.toml → 新增 tree-sitter 依赖

chunker.py 核心逻辑:
  - chunk_file(path, content, language) → list[CodeChunk]
  - 使用 tree-sitter 查询提取 function_definition, class_definition, method_definition
  - 每个 chunk 记录 parent_chunk_id, start_line, end_line
  - 提取 docstring, decorators, imports

dep_graph.py 核心逻辑:
  - build_graph(chunks: list[CodeChunk]) → DependencyGraph
  - 解析 import 语句 → import 边
  - 解析函数调用 → call 边
  - 解析类继承/实现 → inherit 边
  - query_callers(chunk_id) → set[chunk_id]  (谁调用了这个函数?)
  - query_callees(chunk_id) → set[chunk_id]  (这个函数调用了谁?)
  - query_test_files(chunk_id) → set[path]   (哪些测试覆盖了这个代码?)

测试:
  tests/unit/infrastructure/context/test_chunker.py
  tests/unit/infrastructure/context/test_dep_graph.py
  场景:
  - Python 文件切片 (函数/类/方法)
  - JS/TS 文件切片
  - 依赖图构建 (import, call, inherit)
  - 依赖图查询 (callers, callees, test files)
  - 空文件 / 语法错误文件 / 不支持的语言
```

### T5: LanceDB Indexer + Embedding Pipeline

```
依赖: lancedb, voyageai (pip)
新增文件:
  openbot/infrastructure/context/
  ├── indexer.py          # CodeIndexer: chunk → embedding → LanceDB upsert
  ├── embedding.py        # EmbeddingClient: Voyage/OpenAI embedding API
  ├── search.py           # VectorSearch: LanceDB 查询接口
  └── config.py           # ContextConfig: provider, model, budget 等配置

改动:
  pyproject.toml → 新增 lancedb, voyageai 依赖
  openbot/core/settings.py → 新增 context.* 配置项

indexer.py 核心逻辑:
  - index_repo(repo_path, repo_id) → IndexResult (全量)
  - index_files(repo_path, repo_id, file_paths) → IndexResult (增量)
  - 清理已删除文件的 chunk

embedding.py 核心逻辑:
  - embed_code(chunks: list[CodeChunk]) → list[vector]
  - embed_text(texts: list[str]) → list[vector]
  - 支持批量嵌入 (Voyage batch API)
  - 缓存: chunk_id → embedding 映射

search.py 核心逻辑:
  - search_similar(query, repo_id, language, top_k) → list[SearchResult]
  - search_by_dep_graph(chunk_id, repo_id) → list[SearchResult]
  - hybrid_search(query, filters, top_k) → list[SearchResult]
    (向量相似度 + BM25 + SQL 过滤)

LanceDB schema 定义见 §2.6.3

测试:
  tests/unit/infrastructure/context/test_indexer.py
  tests/unit/infrastructure/context/test_embedding.py
  tests/unit/infrastructure/context/test_search.py
  场景:
  - 全量索引 (小仓库 100 文件)
  - 增量索引 (单文件变更)
  - Embedding API 调用 + 缓存
  - 向量检索 top-K
  - 混合检索 (向量 + 过滤)
  - LanceDB 不可用时降级
```

### T6: ContextRetriever + Agent Runtime 集成

```
新增文件:
  openbot/infrastructure/context/
  ├── retriever.py        # ContextRetriever: 检索 + 排序 + 预算分配
  ├── injection.py        # ContextInjectionMiddleware: 注入 agent prompt
  └── fallback.py         # GitHubSearchFallback: 降级到 grep_repo

改动:
  openbot/infrastructure/agents/runtime.py
    → middleware stack 中新增 ContextInjectionMiddleware
  openbot/infrastructure/agents/deepagents_review.py
    → system_prompt() 增加 code context 段
  openbot/infrastructure/agents/deepagents_fix.py
    → system_prompt() 增加 code context 段
  openbot/application/use_cases/review.py
    → AgentRequest 新增 context 字段

retriever.py 核心逻辑:
  - retrieve(repo_id, diff, issue_body, budget) → ContextBundle
  - 步骤:
    1. parse_diff(diff) → changed_files, changed_lines
    2. chunk_mapper(changed_files) → changed_chunks
    3. dep_graph.walk(changed_chunks, hops=1) → related_chunks
    4. vector_search(query_from_diff, top_k=5) → similar_chunks
    5. aggregate + rank + budget_allocate

ContextBundle dataclass:
  code_chunks: list[CodeChunk]       # 相关代码
  related_issues: list[Issue]        # 相关 issues
  related_prs: list[PR]              # 相关历史 PRs
  team_conventions: list[str]        # 团队规范
  total_tokens: int                  # 总 token 数

injection.py 核心逻辑:
  - 在 agent 执行前注入 context
  - Token 预算分配 (P0 > P1 > P2)
  - 超预算时按优先级裁剪

测试:
  tests/integration/context/test_retriever.py
  tests/integration/context/test_injection.py
  场景:
  - 完整检索流程 (diff → context bundle)
  - Token 预算分配 + 裁剪
  - 降级到 GitHub Code Search
  - Context injection 到 agent prompt
  - 无变更文件时的空 context
```
