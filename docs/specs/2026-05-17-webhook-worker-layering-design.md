# OpenBot · Slice F — Webhook / Worker 分层架构设计

> 状态：**Draft · 0.1** · 起草日期：2026-05-17
> 上游：[`../../prd/openbot-prd.md`](../../prd/openbot-prd.md) §5 / §4.5 / §4.7 / §4.8 · [`../../prd/openbot-harness-spec.md`](../../prd/openbot-harness-spec.md) §3 M2 / M3 / M10
> 并行：[`../../plans/2026-05-17-input-side-completeness.md`](../../plans/2026-05-17-input-side-completeness.md)（Slice E，把 chain 补全；本 spec 决定 chain 的"位置"）
> 目标：把 Yi 提出的「webhook 负责决策 + 任务规格化，worker 负责按规格执行」的原则落到具体模块、字段、流转上。

---

## 0. TL;DR — 三句话总结

1. **Webhook 同步段（FastAPI 处理器内）**：HMAC + delivery dedup + state-machine 分类 + 立刻 202。**不**做权限/预算/限流；不调 LLM；不读 `.openbot/config.yaml`。这一段必须 < 1s。
2. **Webhook 异步段（同进程 BackgroundTask 或独立 dispatcher service，逻辑上属于 webhook 层）**：跑「决策 + 规格化」——加载 config、跑所有不会随时间漂移的 gate、可选轻量 LLM 分类器、构建完整 **TaskSpec**。终点要么直接 GitHub 回写后 drop，要么把 TaskSpec 投进 worker queue。
3. **Worker 层**：拿到 TaskSpec 就执行 deep agent。只重检两类会"在 queue lag 中漂移"的状态——cancel 信号 + 累积成本——其余全部信任 webhook 的决策。

> 与现状关系：`openbot.entrypoints.api.app.py` + `state-machine classifier` 已经实现了同步段的一半；`openbot.application.dispatcher.py` 把 10 格 preflight chain 放在了 worker dequeue 之后——本 spec 主张把其中**不会漂移的 7 格**前移到 webhook 异步段，把**会漂移的 3 格**留在 worker。

---

## 1. 分层原则（与 Yi 提案对齐 + 补强）

### 1.1 切分原则（原始）

> Webhook 层负责「决策 + 任务规格化」，Worker 层负责「按规格执行」。Worker 拿到的应该是一份完整、自包含、可直接执行的 TaskSpec——它不需要再判断"这个 issue 是不是 spam"、"这个用户有没有权限"、"现在是不是冷却期"。

**推论 A**：所有可能导致"不需要 worker 介入"的判断都在 webhook 层做。
**推论 B**：所有 LLM 重调用、沙箱、写测试都在 worker 层做。

### 1.2 必要的补强

原始提案漏了**时间漂移**这件事。Webhook 决策 → 入 queue → Worker dequeue 之间可能间隔 0.5s ~ 数分钟（队列积压 / worker 重启 / 重试）。在这段时间里，下列状态可能改变：

| 状态 | 漂移源 | 漂移概率 | 决策位置 |
|---|---|---|---|
| `OPENBOT_KILL_SWITCH` 环境变量 | admin 应急 | 极低 | webhook 决一次 + worker 重检 |
| `features.chat=false`（config） | maintainer PR 改 config | 极低 | webhook 决一次（无需重检——config TTL 60s） |
| Fork PR `/ok-to-test` 评论 | maintainer 后追评 | 中 | webhook 决一次 + 此评论会再发一个 webhook 重新驱动 |
| Actor 角色（owner / collaborator） | 几乎不变 | 极低 | webhook 决一次 |
| Rate limit 计数器 | 自己/他人事件挤兑 | 高 | webhook 决一次（计数器在 webhook 时已是真相） |
| **`cancel-openbot` label** | maintainer 看到 bot 开跑后追加 | **高** | webhook 决 + worker 持续重检 |
| **`@openbot stop` 评论** | 同上 | **高** | webhook 接到该评论后单独 SUPERSEDE/CANCEL；worker 持续 poll |
| **累积 per-task cost** | LLM 调用一次一次累加 | **必然漂移** | worker 内中段 enforcement |
| Global hard kill 累计 | 别的 task 烧的 | 中 | webhook 拒接 + worker 重检 |

→ 结论：分层不是「左 / 右」二选一，而是「**两段 preflight**」：

- **决策段（webhook 异步）**：一次性、确定性、决策后即使外界变化也合理——拿到「该不该接这个任务」的最终答案。
- **重检段（worker 中段 middleware）**：只对那 3 项会持续漂移的状态做 polling。

---

## 2. 完整数据流（更新版 §5.1）

```
GitHub
   │  (webhook delivery)
   ▼
┌──────────────────────────────────────────────────────────────────┐
│ Webhook · 同步段  (openbot.entrypoints.api.app.py @app.post 内联)              │
│ 目标：< 1s 返回 202                                                │
│   1. read raw body                                                │
│   2. verify HMAC                                                   │
│   3. parse → UnifiedEvent                                          │
│   4. WebhookDedup (X-GitHub-Delivery)        ← 已有                │
│   5. dispatch_for(event)        # 纯函数      ← 已有                │
│   6. state-machine classify (intent, run_id, prev_run_id, lock) ← 已有 │
│   7. (PR only) create GitHub check_run       ← 已有                │
│   8. enqueue {raw_event, classification}     ← TaskSpec **未构建** │
│   9. return 202                                                    │
└────────────────────────────────┬─────────────────────────────────┘
                                 │ (intent != IGNORE/CANCEL → 进入下一段)
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ Webhook · 异步段 / Dispatcher  (新建：openbot/dispatcher.py 或     │
│                                  保持 BackgroundTask + 独立 stream)│
│ 目标：构建 TaskSpec OR 直接落地动作 + drop                          │
│   D1. load_for_repo(adapter, event)          ← 已有，前移        │
│   D2. SanitizeInputs                          ← 已有，前移        │
│   D3. KillSwitch                              ← 已有，前移        │
│   D4. FeatureToggle                           ← 已有，前移        │
│   D5. ForkPRGate                              ← 已有，前移        │
│   D6. ActorRole                               ← 已有，前移        │
│   D7. RateLimit                               ← 已有，前移        │
│   D8. MonthlySoftCap + GlobalHardKill 预检    ← 已有，前移        │
│   D9. (业务) classifier (轻 LLM, 可缓存)       ← 新建              │
│   D10. (业务) context enrichment              ← 新建              │
│   D11. (业务) direct-action 短路决策          ← 新建              │
│   D12. 构建 TaskSpec v3 → push to worker queue                    │
│        OR  调用 GitHubGateway 直接回写 → drop                      │
└────────────────────────────────┬─────────────────────────────────┘
                                 │ TaskSpec
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ Worker · 执行段  (openbot.infrastructure.queue/worker.py + workflows/*)          │
│ 目标：按 spec 执行 deep agent，期间只做"漂移重检"                  │
│   W1. dequeue & validate(TaskSpec)                                │
│   W2. mid-execution middleware（每 5 step 一次）：                 │
│       - CancelLabelRefresh   （拉 issue/PR labels）                │
│       - CancelCommentRefresh （Redis cancel:thread:{rid} 查询）    │
│       - BudgetAccumulator     （cost_meter 累加 + 比对 per_task）  │
│       - GlobalHardKillCheck   （Redis 月累计快查）                 │
│       - KillSwitchEnvCheck    （os.environ）                       │
│   W3. workflow handler = DeepAgent + sub-agents per stages_to_run │
│   W4. 中间状态汇报（edit 占位评论，不新发）                          │
│   W5. 自验证（fix 跑 pytest / review 自 critique）                  │
│   W6. GitHubGateway 写回（统一鉴权 + Trufflehog + dedup）          │
│   W7. cost + trace 上报 cost_meter / LangSmith                    │
└──────────────────────────────────────────────────────────────────┘
```

**与 PRD §5.1 的差异**：原图把整条 middleware stack 画在 DeepAgent 里。本 spec 区分两段——只有 W2 那 5 个 polling middleware 留在 agent loop 内；前面 7 个原 chain 项前移到 D2–D8，**对 worker 不可见**。

---

## 3. 各层职责权威清单

### 3.1 Webhook · 同步段（必须 < 1s）

| # | 工作项 | 当前模块 | 是否可见 worker |
|---|---|---|---|
| 1 | HMAC 校验 | `adapters/github.py:verify_signature` | 否 |
| 2 | `X-GitHub-Delivery` dedup | `persistence/dedup.py` | 否 |
| 3 | event 解析为 `UnifiedEvent` | `adapters/github.py:parse_event` | 否 |
| 4 | 路由器 `dispatch_for(event)` | `router.py` | 否 |
| 5 | state-machine 分类（intent + run_id + prev_run_id + resource_lock + CAS） | `state/classifier.py` + `state/runs_repo.py` | **是**（写入 TaskSpec） |
| 6 | PR 上创建 GitHub Check Run（拿 check_run_id） | `webapp.py` | 是 |
| 7 | 跨 dyno cancellation 信号（prev_run_id） | `state/cancellation.py:signal` | 否 |
| 8 | 把 raw event + classification 投递到内部 dispatcher（同进程或独立 stream） | 新建（见 §4.1） | 是 |
| 9 | 立即返回 202 | FastAPI | — |

**绝对禁止**在这一段做的事：

- 任何 GitHub API 读取（除了 `create_check_run`——这是一次"宣示"调用，平均 ~150ms）
- 加载 `.openbot/config.yaml`
- 任何 LLM 调用（包括轻分类器）
- 任何 Postgres 读（state-machine 的 CAS 写除外，它是必要副作用）
- 任何同步等待 > 200ms 的 I/O

### 3.2 Webhook · 异步段 / Dispatcher

**职责**：把「raw event + classification」加工成「可直接执行的 TaskSpec」或「不需要执行的直接动作」。

| # | 工作项 | 性质 | 短路条件 | 当前模块 |
|---|---|---|---|---|
| D1 | 加载 `.openbot/config.yaml`（默认分支 HEAD 的版本，**不**从 PR head） | I/O + cache | — | `config_repo.py:load_for_repo` |
| D2 | SanitizeInputs（unicode Cf/Cc、长度截断、actor 正则） | 纯函数 | 失败 → drop + audit | `middleware/sanitize.py` |
| D3 | KillSwitch（`OPENBOT_KILL_SWITCH=true`） | env 读 | true → drop（silent） | `middleware/cancel.py:KillSwitchMiddleware` |
| D4 | FeatureToggle（`features.{triage,review,fix,chat}=false`） | config 读 | false → drop + audit `feature_disabled` | `middleware/feature_toggle.py` |
| D5 | CancelLabel / CancelComment（**第一次**检查；webhook 收到 cancel 事件单独处理） | GitHub API + regex | 命中 → 不入 queue，发 SUPERSEDE/CANCEL 信号 | `middleware/cancel.py` |
| D6 | ForkPRGate（`security.fork_pr.run=false` + 未见 `/ok-to-test`） | PR 元数据读 | 命中 → announce_once + drop | `middleware/security.py` |
| D7 | ActorRole（fix/chat 的 owner/collaborator 白名单） | GitHub API + cache | 越权 → drop + 评论 | `middleware/security.py` |
| D8 | RateLimit（per_user_per_day + per_repo_per_hour） | Redis 计数 | 超限 → announce_once + drop | `middleware/rate_limit.py` |
| D9 | Budget 预检（monthly_soft + global_hard 快查） | Postgres `cost_meter` | 超限 → drop + 评论 | `middleware/budget.py` |
| D10 | **轻 LLM 分类器**（按场景，见 §3.2.1） | LLM call（一次性、可缓存） | 失败开 → 保守降级 | 新建：`dispatcher/classifier.py` |
| D11 | **轻检索 / 上下文聚合**（issue 全文、相似 issue、PR 元 diff、code owners） | GitHub API + embedding（v0.2） | — | 新建：`dispatcher/context.py` |
| D12 | **Direct-action 短路决策**（template 不合规、信息不全、明显 duplicate、PR 太大、mention 越权澄清） | 纯函数 | 命中 → `GitHubGateway` 写回 → drop | 新建：`dispatcher/direct_actions.py` |
| D13 | 构建 TaskSpec v3 → push to worker queue | 纯函数 + `xadd` | — | 新建：`dispatcher/spec_builder.py` |

> D1–D9 与 Slice E（input-side completeness）完全一致——本 spec 不改它们的实现，只改它们的**运行位置**：从 `dispatch.run_dispatch()`（dequeue 之后）前移到这里（enqueue 之前）。

#### 3.2.1 D10 轻 LLM 分类器在各场景的输出

| 场景 | 分类器输出 | 是否影响 stages_to_run |
|---|---|---|
| Issue triage | `{type, severity_guess, has_reproduction_info, looks_like_spam, dup_candidates: [#N…]}` | 是（决定是否进 reproduce stage） |
| PR review | `{change_size_class, touches_security_paths, is_breaking, suggested_subagents: [correctness/security/arch/docs/tests]}` | 是（决定 stages_to_run 子集） |
| Mention chat | `{intent: readonly_qa / draft_pr / unclear, needs_clarification, scope_hint}` | 是（unclear → 直接回澄清问题，不入 worker） |

**实现要点**：
- 一次性、纯分类、≤ 1.5K input tokens、≤ 500 output tokens
- 用 `claude-sonnet-4-6`（PRD §13 #2），cost 上限 $0.05 / 分类
- **强缓存**：key = `sha256(repo|issue/pr_number|body_hash|classifier_version)`，TTL 1h
- **失败开**：API 超时 / 5xx → 返回 "保守版" 分类（如 review 默认跑全套 sub-agents），并在 spec 标记 `classifier_skipped=true`，worker 不应假定 classifier 一定有

### 3.3 Worker · 执行段

| # | 工作项 | 类别 | 实现 |
|---|---|---|---|
| W1 | TaskSpec 解码与校验 | I/O | `queue/payload.py` + Pydantic schema check |
| W2 | mid-execution polling middleware | safety net | 见 §3.3.1 |
| W3 | 启动对应 deep agent + 注入 `stages_to_run` | execution | `workflows/{triage,review,fix,chat}.py` |
| W4 | 沙箱 checkout / 测试 / 工具调用 | execution | `evals.sandboxes.factory` |
| W5 | 中间汇报（占位评论 edit） | I/O | `GitHubGateway` |
| W6 | 自验证（fix 跑测试 / review self-critique） | execution | agent 内部 |
| W7 | 通过 `GitHubGateway` 最终写回 | I/O | `GitHubGateway` |
| W8 | cost / trace 上报 | I/O | `cost_meter` + LangSmith |

#### 3.3.1 Worker 内 mid-execution middleware（全部本地查询，零 GitHub API）

```
DeepAgent loop:
  for step in run():
      check_per_task_budget()                # cost_meter SELECT（每 step；上次写入即将到上限就停）
      if step.num % 5 == 0:                  # 节流；PRD §4.7 锁定值
          check_kill_switch_env()            # os.environ.get(...)
          check_cancellation()               # redis.SISMEMBER cancel:run:{run_id}  ← 单一 key 覆盖 label + comment
          check_global_hard_kill()           # redis.MGET 月累计
      proceed_step()
```

**关键设计：Redis 驱动而非 GitHub API 轮询**。所有 cancel 触发都通过 webhook 进系统——`issues.labeled (cancel-openbot)` 和 `issue_comment.created (@openbot stop)` 都是 first-class 事件，webhook 同步段在 state-machine 分类为 CANCEL 时直接 `cancellation.signal(redis, run_id)`。worker 端只需查一个 Redis key。

| 触发 | webhook 处理 | worker polling |
|---|---|---|
| `cancel-openbot` label | classifier 分类为 CANCEL → 写 `cancel:run:{run_id}` | `SISMEMBER cancel:run:{run_id}` |
| `@openbot stop` 评论 | 同上 key | 同上 |
| `OPENBOT_KILL_SWITCH=true` | webhook 同步段 KillSwitch middleware 直接 drop（不入 queue） | `os.environ.get(...)`（worker 启动时即可能改） |

**与现有代码的差距**：
- `state/cancellation.py` 的 `signal()` 已具备，无需改造
- `state/classifier.py` **需扩展**——目前不处理 `ISSUE_LABELED` / `ISSUE_UNLABELED` / `PR_LABELED` / `PR_UNLABELED`。要加：

```
| ISSUE_LABELED   (label==cancel-openbot)   | RUNNING        | CANCEL  |
| ISSUE_LABELED   (其他 label)               | *              | IGNORE  |
| ISSUE_UNLABELED (label==cancel-openbot)   | *              | IGNORE  |
| PR_LABELED / PR_UNLABELED (同样匹配)      | 同上            | 同上    |
```

cancel label name 来自 `EffectiveConfig.cancel.label`（默认 `cancel-openbot`，可配）。同步段不能加载 config，所以让 classifier 接受 `cancel_label_name: str` 参数，由 webhook 异步段在 D1 加载完 config 后回填给一个轻量同步段 helper——或者退而求其次：v0.1 锁定为 `cancel-openbot` 字面量，配置项的"自定义 label name"推 v0.2。

**TaskSpec 初始 label 快照**：worker 拉到 spec 的那一刻，cancel-openbot label 可能已经在 issue 上（webhook 入 queue 与 worker dequeue 之间打的，对应的 `issues.labeled` 事件被处理 vs worker pickup 有竞态）。所以 TaskSpec 必带 `target.initial_labels: tuple[str, ...]`——webhook 异步段构建 spec 时拉一次的快照——worker 启动 W1 校验阶段先比对一次，命中即立刻 stop。

**对比 §3.2 D5/D9**：webhook 侧的 D5（CancelLabel/CancelComment 检查）和这里 worker polling 的 cancel 检查**查的是同一个 Redis key**，只不过 D5 是"webhook 收到事件那刻一次性查"，worker polling 是"持续查直到任务结束"。webhook drop 时 GitHub 会 retry（30 天）；如果真要纵深防御，可以推 F4 加每 100 step 一次的 GitHub API 调和兜底——v0.1 不做。

**为什么 ActorRole / FeatureToggle / RateLimit / ForkPRGate 等不在 worker 重检**：
- 角色不会在 30s 内变
- config 60s TTL，且 worker 也读不到 webhook 时的精确版本（除非把 config raw 也塞进 spec——见 §4.4）
- rate limit 计数器只在事件接收时有意义
- fork PR 状态不变；`/ok-to-test` 评论会触发新 webhook 重新走 D1–D13

---

## 4. TaskSpec v3 契约（worker 的输入合同）

### 4.1 当前 v2 vs 提议 v3

当前 `QueuePayload v2` (`queue/payload.py`) 携带：

```
version, channel, delivery_id, kind, repo, actor, actor_type,
issue_number, pr_number, comment_body, installation_id, raw,
feature, task_id, enqueued_at, check_run_id,
intent, run_id, prev_run_id, resource_key, event_seq
```

——这是个"webhook 事件的 JSON 镜像 + 分类结果"。worker 仍要 `load_for_repo` 再跑 10 格 chain。

**v3 要补齐的**：webhook 已经做完的决策与已经拉好的上下文，让 worker 不必重做。

### 4.2 v3 完整 schema

```python
@dataclass(frozen=True, slots=True)
class TaskSpec:
    # ── 身份 ──
    spec_version: int = 3
    task_id: str            # 来自 derive_task_id（delivery 维度，幂等键）
    run_id: str             # 来自 derive_run_id（resource 维度，cancellation 键）
    prev_run_id: str | None
    resource_key: str       # github:owner/repo:issue:42 / :pr:7
    event_seq: int
    intent: Literal["start", "supersede"]   # cancel/ignore 不会到这里
    enqueued_at: str        # ISO8601 UTC
    spec_built_at: str      # ISO8601 UTC（用于 stale 检测）

    # ── 触发 ──
    scenario: Literal["triage", "review", "fix", "chat"]
    target: Target          # repo / issue_number or pr_number / head_sha / base_sha

    # ── 决策（worker 信任，不重判） ──
    decision_trace: list[DecisionStep]   # 每个 D2–D9 的 outcome（PROCEED / SKIPPED reason）
    classifier_outputs: ClassifierBundle | None    # D10 输出；可能 None（failed-open）
    classifier_skipped: bool             # True 时 worker 默认跑全套 stages

    # ── 执行规格 ──
    stages_to_run: list[Stage]           # 子 agent 序列（按 scenario 而异）
    budget: BudgetEnvelope               # per_task_usd / max_wallclock_s / max_subagent_calls / max_steps
    constraints: Constraints             # write_actions_allowed / branch_naming_rule / severity_threshold / language

    # ── 上下文（webhook 拉好的 read-only 包） ──
    context_bundle: ContextBundle        # 见 §4.3，按 scenario 不同 schema

    # ── 回写指向 ──
    callback: CallbackTarget             # 把回写动作精确指向某个 thread / comment / check_run
```

```python
@dataclass(frozen=True, slots=True)
class Target:
    repo: str                # owner/repo
    installation_id: int
    issue_number: int | None
    pr_number: int | None
    head_sha: str | None     # PR 专用，spec 锁定时的 sha
    base_sha: str | None     # PR 专用
    last_reviewed_sha: str | None       # incremental review：上一轮 review 落在的 sha
    initial_labels: tuple[str, ...]     # spec 构建瞬间的 label 快照；worker W1 比对兜底

@dataclass(frozen=True, slots=True)
class DecisionStep:
    middleware: str          # "kill_switch" / "feature_toggle" / ...
    outcome: Literal["proceed", "blocked", "skipped"]
    reason: str | None
    latency_ms: int

@dataclass(frozen=True, slots=True)
class Stage:
    name: str                # e.g. "reproduce" / "correctness_review" / "patch" / "self_fix"
    enabled: bool
    inputs: dict[str, Any]   # stage 专用的 hint

@dataclass(frozen=True, slots=True)
class BudgetEnvelope:
    per_task_usd: Decimal
    max_wallclock_s: int
    max_subagent_calls: int
    max_steps: int
    max_self_fix_attempts: int   # fix 专用，PRD §4.3 锁 3

@dataclass(frozen=True, slots=True)
class Constraints:
    write_actions_allowed: frozenset[str]   # {"reply", "label", "create_branch", "create_pr", "edit_comment"}
    branch_naming_rule: str | None          # e.g. "openbot/{issue_num}-{slug}"
    severity_threshold: SeverityThreshold
    language: str | None                    # comment_language: auto 时 None；其余锁定

@dataclass(frozen=True, slots=True)
class CallbackTarget:
    reply_thread: str        # github:owner/repo:issue:42:comments
    placeholder_comment_id: int | None    # 已经发的"working on it" 评论 id，让 worker edit 而非新发
    check_run_id: int | None
```

### 4.3 ContextBundle 按场景定型

```python
# triage
@dataclass(frozen=True, slots=True)
class TriageContext:
    issue_title: str         # sanitized
    issue_body: str          # sanitized
    issue_labels: tuple[str, ...]
    issue_template_violations: tuple[str, ...]    # webhook 已经核对的
    similar_issues: tuple[SimilarIssueRef, ...]   # 最多 5 个，v0.1 留空，v0.2 接入 pgvector
    classifier: TriageClassifierOutput            # type / severity_guess / has_reproduction_info / looks_like_spam

# review
@dataclass(frozen=True, slots=True)
class ReviewContext:
    pr_title: str
    pr_body: str
    diff_scope: DiffScope        # incremental: last_reviewed_sha → head_sha；first time: base..head
    is_incremental: bool
    is_force_push: bool          # before_sha 不是 last_reviewed_sha 的祖先
    touched_files: tuple[str, ...]
    touched_paths_classify: PathClassify    # 命中 security_sensitive / docs_only / ...
    code_owners_for_diff: tuple[str, ...]
    related_issues: tuple[int, ...]
    prior_findings_fingerprint: tuple[str, ...]    # 用于增量去重
    classifier: ReviewClassifierOutput

# fix
@dataclass(frozen=True, slots=True)
class FixContext:
    issue_body: str
    triage_summary: str | None    # 来自之前 triage task 的 result（如果有）
    issue_labels: tuple[str, ...]
    continuation_from: FixContinuation | None    # CI 失败回跑时携带上一轮的失败信息
    classifier: FixClassifierOutput | None       # 可选

# chat
@dataclass(frozen=True, slots=True)
class ChatContext:
    full_mention_body: str       # sanitized
    thread_history: tuple[CommentRef, ...]   # 同一 thread 的历史 @openbot 对话
    pr_or_issue_meta: dict[str, Any]         # 取决于 mention 在哪
    classifier: ChatClassifierOutput         # intent + needs_clarification + scope_hint
```

### 4.4 版本兼容

- `spec_version=2` 的旧 entries → worker 仍能解（兼容路径：自动填 `decision_trace=[]`、`classifier_skipped=true`、`stages_to_run` 默认全套、`context_bundle` 现场拉取）。这给 webhook/worker 滚动升级留窗口期。
- `spec_version=3` 是本 spec 落地后的目标。一旦 webhook 全量升级，worker 把 v2 兼容路径作 deprecation warning。

---

## 5. 四场景 Pipeline（每一步标注落点）

### 5.1 Triage（`issue.opened`）

| 步骤 | 落点 | 已有模块 / 新建 |
|---|---|---|
| HMAC + parse + dedup | webhook 同步 | 已有 |
| state-machine 分类 | webhook 同步 | 已有 |
| `enqueue 内部 dispatcher` | webhook 同步 | 新建（或保持 BackgroundTask） |
| **D1–D9** 决定性 gate | webhook 异步 | 前移现有 middleware |
| Template / 必填字段合规检查 | webhook 异步 | 新建：`dispatcher/templates.py` |
| 拉 issue 全文 + comments | webhook 异步 | 新建：`dispatcher/context.py:fetch_issue` |
| **D10 轻 LLM 分类**：type / severity / has_repro / looks_like_spam | webhook 异步 | 新建（v0.1 用 sonnet-4-6） |
| 相似 issue 检索（pgvector + rerank） | webhook 异步 | **v0.2**（v0.1 留接口） |
| **D12 直接落地**：spam → 关 + 评论；info 不全 → `needs-info` label + 追问；明显 dup → 关联评论 | webhook 异步 → `GitHubGateway` → drop | 新建：`dispatcher/direct_actions.py` |
| 构建 TaskSpec：`stages_to_run = ["classify_labels", "reproduce", "summarize"]`（其中 reproduce 仅在 `has_repro=true && type=bug` 时开启） | webhook 异步 | 新建 |
| **沙箱复现** | worker | `workflows/triage.py` + deep agent |
| 根因假设 + 影响面 | worker | deep agent |
| 双受众 summary（OP + 维护者） | worker | deep agent |
| 落 label + 写回 summary | worker → `GitHubGateway` | 已有 adapter，封装 gateway |

### 5.2 PR Review（`pull_request.opened` / `.synchronize`）

| 步骤 | 落点 | 备注 |
|---|---|---|
| HMAC + parse + dedup | webhook 同步 | 已有 |
| state-machine 分类 → `pr.synchronize` 在 RUNNING 时为 SUPERSEDE | webhook 同步 | 已有 |
| 创建 GitHub Check Run（`in_progress`） | webhook 同步 | 已有 |
| 跨 dyno cancel signal 给 prev_run_id | webhook 同步 | 已有 |
| **D1–D9** + Fork PR gate | webhook 异步 | 前移 |
| 元数据检查：draft / fork / size_class | webhook 异步 | 新建：`dispatcher/pr_meta.py` |
| **D10 轻 LLM**：change_size_class / touches_security_paths / is_breaking | webhook 异步 | 新建 |
| **关键**：incremental scope 计算 = `last_reviewed_sha → head_sha` | webhook 异步 | 新建：`dispatcher/incremental.py` |
| Force-push 检测（`before_sha` 不是 `last_reviewed_sha` 的祖先）→ 退回全量 | webhook 异步 | 同上 |
| 决定 sub-agents 集合：默认 `["correctness"]`；touches_security_paths → 加 `"security"`；改 `docs/` → 加 `"docs"` | webhook 异步 | 写进 `stages_to_run` |
| **D12 直接落地**：超大 PR (>500 行) → 评论建议拆分 + 不入 queue（或入 queue 但 stages 只剩 docs） | webhook 异步 | 新建 |
| 深度 review + 子 agents 推进 | worker | deep agent |
| 历史 finding 去重（用 `prior_findings_fingerprint`） | worker | 在 agent 内部 |
| Summary + inline comments 组装 + 写回 | worker → `GitHubGateway` | 用 `commit_id = target.head_sha` 锁定行号 |
| 更新 Check Run conclusion | worker → adapter | 已有 |

> §5.2 的关键决策"用哪些 sub-agent"由 webhook 决定（写进 `stages_to_run`）——这是 Yi 提案最重要的一条收口。

### 5.3 Fix（`issue.assigned to bot` / CI 续跑）

| 步骤 | 落点 |
|---|---|
| HMAC + parse + dedup + state-machine | webhook 同步 |
| **硬权限**：assignee 必须是 owner/collaborator（PRD §4.3） | webhook 异步（ActorRole）|
| **D1–D9** + Budget 预检（fix 最贵，$3.00） | webhook 异步 |
| Fetch issue + 上次 triage 结果（如果有，从 `task_runs` 关联） | webhook 异步 |
| **D10 轻 LLM**（可选）：复现可行性 / 涉及模块猜测 | webhook 异步 |
| 即时 ACK 评论（用 placeholder_comment_id 给 worker 后续 edit） | webhook 异步 → `GitHubGateway` |
| 构建 spec：`stages_to_run = ["plan", "read", "patch", "test", "self_fix"]`；`max_self_fix_attempts=3` | webhook 异步 |
| 沙箱 clone + agent loop | worker |
| Push branch + 开 PR (`draft=false`) | worker → `GitHubGateway` |
| CI 失败 → 续跑流：GitHub 发 `check_run.completed (failure)` → 走 webhook 流程 → state-machine 分类为 SUPERSEDE（同 issue 在 RUNNING）→ 构建 `continuation_from=FixContinuation(prev_run_id, failed_checks)` 的 spec → worker | webhook 异步 **统一入口** |
| 永不 auto-merge | worker（写死的 constraint） |

> **CI 续跑放 webhook 统一入口**是 Yi 提案的自然延伸——这样 worker 不需要"自我重新调度"，所有任务的发起都经过 webhook 异步段。

### 5.4 @mention Chat（含澄清环）

| 步骤 | 落点 |
|---|---|
| HMAC + parse + dedup + state-machine（chat 同 thread 上一次跑没结束 → SUPERSEDE） | webhook 同步 |
| 权限 gate（`chat.allow_anyone` 或 collaborator+） | webhook 异步（ActorRole） |
| RateLimit | webhook 异步 |
| Fetch thread 历史（同一 issue/PR 下之前的 @openbot 对话） | webhook 异步 |
| **D10 轻 LLM**：intent ∈ {readonly_qa, draft_pr, unclear, out_of_scope} + needs_clarification | webhook 异步 |
| **D12 直接落地**：unclear → webhook 直接发追问评论（**不入 worker**），等下一轮 @openbot 再走流程 | webhook 异步 → `GitHubGateway` → drop |
| **D12 直接落地**：out_of_scope / 越权（如 `@openbot merge`）→ 礼貌拒绝评论 → drop | 同上 |
| **D12 直接落地**：CoC 违规语言检测命中 → mute thread | 同上 |
| 构建 spec：`stages_to_run = ["readonly_qa"]`（或 future `["draft_pr"]`） | webhook 异步 |
| Chat agent 只读工具集执行 | worker |
| 编辑占位评论给出最终回答 | worker → `GitHubGateway` |

> Yi 的提案在 chat 这里特别有价值：澄清环放在 webhook，避免 worker 跑到一半发现方向错。

---

## 6. 关键边缘 / 横切关注

### 6.1 Cancellation 的三条路径如何穿过两层

| 触发 | webhook 收到的事件 | webhook 同步段（state-machine） | webhook 异步段 | worker polling |
|---|---|---|---|---|
| label `cancel-openbot` 加上 | `issues.labeled` / `pull_request.labeled` | classifier 在 RUNNING 状态识别该 label name → CANCEL intent → `cancellation.signal(redis, run_id)` 写 `cancel:run:{run_id}` | （不进入；intent=CANCEL 直接 ack） | `SISMEMBER cancel:run:{run_id}` 命中 → 优雅退出 |
| 评论 `@openbot stop` 等关键词 | `issue_comment.created` | 同上（关键词来自 `config.cancel.comment_keywords`，v0.1 锁字面量） | 同上 | 同上（同一 Redis key） |
| env `OPENBOT_KILL_SWITCH=true` | — | （不通过 webhook；admin SSH 改的） | — | worker polling 每 5 step `os.environ` 读 |

**重点**：
- cancel 事件**永远不入 worker queue**——它们只产生 cancellation signal。worker 端只 poll Redis + env，**不**调 GitHub API。
- 单一 Redis key `cancel:run:{run_id}` 覆盖 label 与 comment 两条路径，worker 不必区分。
- TaskSpec 必带 `target.initial_labels` 快照，worker W1 校验阶段比对一次——抓 webhook → worker 之间的 cancel 漂移（也覆盖了"label 在 task 入 queue 前就已经加了"这种 ordering 问题）。
- GitHub webhook 投递丢失的兜底（每 100 step 一次 GitHub API 调和）推 F4，v0.1 不做。

**对 classifier 的扩展（必做）**：当前 `state/classifier.py` 不处理 `ISSUE_LABELED` / `PR_LABELED`。要加：

```
| ISSUE_LABELED / PR_LABELED   (label==cancel.label)   | RUNNING  | CANCEL  |
| ISSUE_LABELED / PR_LABELED   (其它 label)             | *        | IGNORE  |
| ISSUE_UNLABELED / PR_UNLABELED (cancel.label)        | *        | IGNORE  |
```

### 6.2 Config 在哪个 SHA 加载

**统一锁定**：webhook 异步段加载默认分支 HEAD 的 `.openbot/config.yaml`。

为什么不是 PR head？因为 PRD §4.8 锁定"config 改动需 admin 加 `config-approved` label 才生效"——PR 内的 config 改动不能影响 review/fix 本身（自己改自己的预算用 bot 跑自己）。

config 不进 TaskSpec 主体（避免 spec 太大），但**关键决策字段**进 `Constraints`：
- `severity_threshold`
- `language`
- `branch_naming_rule`
- `write_actions_allowed`

worker 默认信任 spec.constraints，不再读 config。

### 6.3 Force-push / Supersede / Incremental 的互动

```
event: pr.synchronize, before=X, after=Y
state-machine:
  current state = RUNNING (last_reviewed_sha=W)
  classify: SUPERSEDE
webhook 异步段：
  compute_diff_scope:
    if W is ancestor of X (fast-forward):
       diff_scope = (W, Y), is_incremental=true, is_force_push=false
    else:
       diff_scope = (base_sha, Y), is_incremental=false, is_force_push=true
       # 不复用 prior_findings_fingerprint
  build spec → enqueue
旧 worker：cancellation signal → 退出
新 worker：按 spec 跑
```

> `last_reviewed_sha` 来自 `task_runs.metadata.last_reviewed_sha`——worker 完成时回写。

### 6.4 Light-LLM 调用的预算与缓存

D10 的 LLM 调用属于"webhook 决策成本"，与 worker 执行成本分开记账：

| 项 | 上限 | 模型 | 缓存 |
|---|---|---|---|
| classify_issue | $0.05 / call | sonnet-4-6 | sha256(repo|issue#|body|version) TTL 1h |
| classify_pr | $0.05 / call | sonnet-4-6 | 同上，加 head_sha |
| classify_mention | $0.03 / call | sonnet-4-6 | sha256(repo|thread_id|body|version) TTL 5m |

- 单实例每月预算独立项：`dispatcher_llm_monthly_usd: 30`（建议默认值；可配置）
- 超 `dispatcher_llm_monthly_soft_cap` → 后续 dispatch 跳过 D10，spec 标 `classifier_skipped=true`，worker 按全量 stages 跑（fail-open 到"贵但正确"，而不是"便宜但跑错"）

### 6.5 Concurrency / Single-flight

webhook 同步段已经有 `resource_lock(redis, resource_key)`，确保同一 issue/PR 的两个 webhook delivery 不同时进 state-machine。但**异步段不持锁**——SUPERSEDE intent 本身就允许新 run 在旧 run 没退之前进入；旧 run 的退由 cancellation signal 异步驱动。

worker queue 的并发由 `OPENBOT_WORKER_CONCURRENCY` 控制，per-scenario 物理隔离推到 §6.6。

### 6.6 Worker 池按 scenario 分（v0.1 留接口，v0.2 上）

```yaml
worker_pools:
  triage:    { concurrency: 4, max_wallclock_s: 600  }
  review:    { concurrency: 4, max_wallclock_s: 600  }
  fix:       { concurrency: 2, max_wallclock_s: 2700 }
  chat:      { concurrency: 8, max_wallclock_s: 180  }
```

实现：stream 仍叫 `openbot.application.workflows`，但 consumer group 分 4 个：`openbot.application.workflows:group:{scenario}`。enqueue 时 `xadd` 后立刻 `XCLAIM` 不通；改用 4 个独立 stream `openbot.application.workflows:{scenario}` 更干净。

webhook 异步段把 spec 投到对应的 stream——这个映射是 spec.scenario → stream_name 的纯函数。

### 6.7 GitHubGateway（写回的最后一关）

无论 webhook 异步段直接落地，还是 worker 写回，**所有 GitHub 写动作走同一出口**：

```python
class GitHubGateway:
    async def reply(self, target: CallbackTarget, body: str, *, allowed_actions: frozenset[str]) -> int: ...
    async def label(self, target: Target, labels: list[str]) -> None: ...
    async def edit_comment(self, comment_id: int, body: str) -> None: ...
    async def create_pr(self, target: Target, head_branch: str, ...) -> int: ...
    async def update_check_run(self, ...) -> None: ...
```

Gateway 内做：
1. 鉴权（installation token 缓存）
2. **最终权限校验**：`action in allowed_actions`（write_actions_allowed 来自 spec.constraints）—— defence in depth
3. **Trufflehog 扫描出站文本**（PRD §4.8）；命中即 redact
4. **去重 / idempotent 写**：comment 用 `external_id`-style header；label 写前先查
5. **GitHub API 限速 backoff**
6. 写 audit_log（每一个写动作一行）

---

## 7. 与 spec / 已有 plan 的关系

### 7.1 与 Slice E（input-side-completeness）

Slice E 在补 chain 的内容（`SanitizeInputs`、`AuditStart`、`FeatureToggle` 三 middleware + lint + e2e）。本 spec **不与 E 冲突**——E 关注"chain 完不完整"，本 spec 关注"chain 放哪一层"。

执行顺序：
1. **先把 E 完成**（chain 内容齐全），仍在 worker 跑
2. **再做本 spec 的 F1**（chain 前移），不改内容只改位置
3. **F2 / F3 加业务**（轻 LLM、context 聚合、direct actions、TaskSpec v3）

### 7.2 与 `webhook-worker-test-plan.md`

测试计划里 I / P / M / X 系列大部分仍然有效，本 spec 只移动了某些状态转换的边界：

| 测试 ID | 旧验收（中段在 worker） | 新验收（中段在 webhook） |
|---|---|---|
| P-20 fork PR → review 只读 | ForkPRGateMiddleware 在 worker 拦截 | ForkPRGate 在 webhook **不入 queue** |
| M-30 非 collaborator → 拒绝 | ActorRoleMiddleware 在 worker 拦截 | 同上 |
| I-08 cancel-openbot label → cancel | CancelLabel 在 worker | webhook + worker 双层（worker 是 polling 兜底） |

需要补充的 case：
- **F-01**：webhook 异步段 LLM classifier 超时 → spec.classifier_skipped=true，worker 按全套 stages 跑
- **F-02**：webhook 直接落地（spam / duplicate / too-big PR）→ 没有 task 入 queue，audit 表只有 SKIPPED 行
- **F-03**：webhook 决策时 PROCEED，worker dequeue 时发现 cancel label 已加 → polling middleware 命中
- **F-04**：webhook 异步段 dispatcher 进程崩溃 → BackgroundTask 失败 / dispatcher stream 重投递；同一 delivery_id 经 dedup 仍只产生一个 TaskSpec
- **F-05**：fix CI 失败续跑 → state-machine SUPERSEDE → spec 携带 `continuation_from`

### 7.3 必要的 spec amendments（land 后回填 `openbot-harness-spec.md`）

| 锚点 | 改动 |
|---|---|
| §3 M3 | 把 chain 拆为「decision chain（webhook 异步）」+「polling chain（worker mid-execution）」两段；列清楚各自的成员 |
| §3 M10 | QueuePayload v2 升级到 TaskSpec v3；详列字段 |
| §5 / §9 | 增加 `openbot/dispatcher/` 子包；新模块：`spec_builder`、`classifier`、`context`、`direct_actions`、`incremental` |
| §9.1 | `derive_task_id` / `derive_run_id` 不变 |
| §9.3 | worker 内中段 polling 节流：5 step / 30s 取小者（原 PRD §4.7 锁定） |
| §3 新 M | `GitHubGateway` 作为出站统一关 |

---

## 8. 实施切片（F1 → F3）

每个切片可独立 land + 端到端 demo-able + 不破坏现有路径。

### F1 — Chain 前移（不增业务）

**范围**：把 Slice E 后稳定的 10 格 chain 中的 7 格从 `dispatch.run_dispatch()` 前移到新模块 `openbot/dispatcher/preflight_decision.py`，在 webhook BackgroundTask 中先跑；worker 仍保留 3 格 polling middleware。

**验收**：
- 跑 demo 1–9（spec §7）：全部仍绿
- 新 demo：跑一个 `feature.chat=false` 的 issue，audit 表里**不再有"陈年 STARTED 跟 SKIPPED"**——只有 SKIPPED；worker queue 完全没有该 entry
- p50 ack-to-decision latency < 1.5s（含 LLM 不开）
- TaskSpec v3 schema 通过 Pydantic 校验

**预估**：3 天

### F2 — Direct-action 短路 + 上下文聚合

**范围**：新建 `dispatcher/direct_actions.py` + `dispatcher/context.py`；triage 上 template / 信息不全检查；review 上 oversized PR 建议拆分；mention 上澄清环。

**验收**：
- Demo：开一个空 body 的 issue → bot 不入 worker queue，只发 needs-info 评论
- Demo：开一个 800 行的 PR → bot 不入 queue，发"建议拆分"评论
- Demo：评论 `@openbot 改一下吧` → bot 直接追问"你想改什么"，不入 queue
- F-02 测试通过

**预估**：3 天

### F3 — 轻 LLM classifier + incremental review

**范围**：新建 `dispatcher/classifier.py`（一次性 sonnet-4-6 分类）+ `dispatcher/incremental.py`（last_reviewed_sha → head_sha + force-push 检测）；spec 携带 `stages_to_run`；worker 按 stages 选择 sub-agents。

**验收**：
- review 上 docs-only PR → spec.stages_to_run = ["docs"]；其他 sub-agent 不启动
- review 上 security-sensitive PR → 加跑 security sub-agent
- 第二次 push → spec.is_incremental=true，diff_scope 缩小到增量
- force-push → spec.is_force_push=true，退回全量
- F-01 / F-05 测试通过

**预估**：5 天

### F4（推 v0.2）

- Issue 相似检索 / dedup（pgvector + rerank）→ webhook 异步段
- Per-scenario worker pool 物理隔离（4 个独立 stream）
- `GitHubGateway` 抽出为独立模块（v0.1 阶段先封装 adapter 的写方法）

---

## 9. 决议表（本 spec 锁定的关键点）

| # | 决策 | 值 | 理由 |
|---|---|---|---|
| 1 | preflight chain 切分 | 7 格在 webhook 异步、3 格在 worker polling | 时间漂移分析（§1.2 表） |
| 2 | TaskSpec 版本 | v3 | v2 不带 `stages_to_run` / `context_bundle` / `constraints` |
| 3 | dispatcher 实现形式（v0.1） | webhook 进程内 BackgroundTask（或独立 stream `openbot.application.dispatcher`） | 不引入新服务以保持 docker-compose 简单 |
| 4 | dispatcher LLM 模型 | claude-sonnet-4-6 | PRD §13 #2 |
| 5 | dispatcher LLM 月预算 | $30 / instance（可配） | $1 / 天，覆盖 ~600 次分类 |
| 6 | classifier 失败降级 | fail-open，全套 stages | 贵但正确 > 便宜但跑错 |
| 7 | config 加载 SHA | 默认分支 HEAD | PRD §4.8 config-approved 锁定 |
| 8 | cancel 事件不入 worker queue | 锁定 | 仅产生 cancellation signal（写 Redis key） |
| 8a | worker 检测 cancel 的方式 | Redis SISMEMBER（不调 GitHub API） | webhook 已经是 first-class 入口，worker 主动 poll API 既慢又烧 quota |
| 8b | classifier 扩展 ISSUE_LABELED / PR_LABELED | 锁定 | 让 label-cancel 走与其它 cancel 同一条 state-machine + signal 路径 |
| 9 | CI 失败续跑 | 经 webhook 统一入口 | 不让 worker 自我调度 |
| 10 | GitHubGateway | 统一出口（webhook 直接落地 + worker 写回共用） | defence in depth + Trufflehog |

---

## 10. 非目标（明确推后）

- LangGraph agent loop 实现细节（agent slice 的工作）
- Sandbox 接入（Modal / Daytona / Docker—— `evals.sandboxes.factory`）
- Linear / Slack / Discord adapter（v0.2+）
- 多租户托管 / OpenBot Cloud（v1.0+）
- `.openbot/config.yaml` 校验 CLI
- pgvector / 相似 issue（推 F4）
- Per-scenario worker pool 物理隔离（推 F4）

---

## 11. 引用

- 上游 PRD：[`../../prd/openbot-prd.md`](../../prd/openbot-prd.md) §4.5 / §4.6 / §4.7 / §4.8 / §5.1
- 上游 harness spec：[`../../prd/openbot-harness-spec.md`](../../prd/openbot-harness-spec.md) §3 M2 / M3 / M10
- 并行 plan：[`../../plans/2026-05-17-input-side-completeness.md`](../../plans/2026-05-17-input-side-completeness.md)（Slice E）
- 并行 plan：[`../../plans/webhook-worker-test-plan.md`](../../plans/webhook-worker-test-plan.md)（测试矩阵）
- 现有代码：
  - `openbot.entrypoints.api.app.py`（同步段）
  - `openbot.application.router.py`（dispatch_for / derive_task_id / derive_run_id）
  - `openbot.application.state/classifier.py`（state machine）
  - `openbot.application.state/runs_repo.py`（CAS / 锁）
  - `openbot.application.state/cancellation.py`（跨 dyno signal）
  - `openbot.application.dispatcher.py`（**待改造**：chain 前移）
  - `openbot.infrastructure.queue/{payload,worker}.py`（**待改造**：spec v3）
  - `openbot.application.middleware/*.py`（**待复用**：从 worker 搬到 dispatcher）
