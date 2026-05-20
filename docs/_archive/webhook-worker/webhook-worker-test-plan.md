# Webhook-Worker 测试计划

> **状态**: Archived — Phase 1 完成（2026-05-20）；Phase 2 部分完成；Phase 3 显式推后到 v0.2。
> **归档原因**: Phase 1 MVP 已落地（733 tests passing），测试 ID 映射保留在测试文件 docstring 中（见下方"实测落地"）。剩余 Phase 2/3 在 v0.2 评审时重启。
> **范围**: `openbot/` webhook 入口 → Redis dispatch → worker workflow
> **依赖文档**: `docs/prd/openbot-prd.md` §3, §4, §8; `openbot/application/dispatcher.py` middleware chain

## 实测落地（Phase 1 / v0.1 MVP）

| 测试 ID | 落地文件 |
|---|---|
| I-01, I-02, I-03, I-05x2, I-09, I-11, I-23, M-31 | `tests/state_machine/test_issue_lifecycle.py` |
| P-01, P-02x2, P-03, P-05 | `tests/state_machine/test_pr_lifecycle.py` |
| I-30, I-31x2, I-32, X-01 | `tests/state_machine/test_error_paths.py` |
| I-24 Redis 写顺序、I-20 supersede、I-33 worker 恢复 | `tests/integration/{test_redis_ordering,test_concurrent_supersede,test_worker_recovery}.py` |
| P-20..P-23 fork-PR 安全门 | `tests/application/middleware/test_security.py`（13 cases；plan-ID map 见该文件 docstring） |
| 取消/超时/budget 边界 | `tests/state/test_cancellation.py`、`tests/application/middleware/test_budget.py` |

## 未落地（v0.2 评审时重启）

- **Phase 2 剩余**: I-21（worker 中途 supersede 优雅退出）、I-25/26（pickup-check + cancel-check 时序）、P-52..P-57（多 commit 细节、force-push、base 更新）、M-20..M-23（chat stream 隔离）、X-02 budget cap / X-04 stream TTL。
- **Phase 3（v0.2）**: 增量 review（P-60..P-64）、fix workflow 细节（P-10..P-13）、评论反馈循环（M-40..M-43）、dashboard/LangSmith/token-cache 观测（X-05..X-07）。
- 当前路由 `dispatch_for` 不路由 ISSUE_CLOSED / PR_CLOSED / PR_MERGED → I-04 / P-04 取消路径需先扩展路由。

---

---

## 一、测试分层总览

```
┌─────────────────────────────────────────────────┐
│  L4: 真实 GitHub + smee.io → 本地服务           │  ← 端到端，手动触发
├─────────────────────────────────────────────────┤
│  L3: Mock GitHub server + 真实 Redis/Postgres   │  ← 集成测试，并发场景
├─────────────────────────────────────────────────┤
│  L2: 本地组件 + 手动 curl 注入 webhook payload  │  ← 状态机 + middleware chain 测试
├─────────────────────────────────────────────────┤
│  L1: 单元测试，纯函数 + mock Redis              │  ← 最快，最局部
└─────────────────────────────────────────────────┘
```

### 分层原则

| 层 | 测什么 | 不测什么 | 自动化 |
|---|---|---|---|
| **L1** | 事件解析、intent 分类、幂等 key 计算、状态转换纯函数 | 网络 I/O、存储副作用 | CI 必跑，< 5s |
| **L2** | 完整 middleware chain、webhook 路由决策、状态机转换 | LLM 调用、GitHub API | CI 必跑，< 30s |
| **L3** | Redis Stream + Postgres 的事务正确性、并发竞态 | 真实 GitHub 事件 | CI 按需跑，< 5min |
| **L4** | 真实 GitHub → smee → 本地 → workflow 全链路 | 自动化（需要真实仓库） | 手动，发布前验证 |

### 每层的验收标准思路

- **L1**: 给定输入 → 断言输出，不依赖任何外部状态，测试数量最多
- **L2**: 给定 HTTP POST payload → 断言 Redis key 变化 + 队列状态，用 `fakeredis`
- **L3**: 启动真实 Redis + Postgres→ 并发注入多个 webhook → 断言最终一致性
- **L4**: 用 smee.io 转发真实 GitHub 事件 → 人工核对 bot 行为是否符合预期

---

## 二、Issue 场景测试

### A. 单事件生命周期（L1/L2）

核心思路：每个 `issues.*` action 都有一个确定的**意图**（START / SUPERSEDE / CANCEL / IGNORE），测试目标是验证意图识别和状态机转换正确。

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **I-01** | `issues.opened` | 触发新 triage 任务 | Redis stream 指针建立；队列有新 task；task 状态为 `pending` |
| **I-02** | `issues.edited`（无在跑任务） | 与 opened 等价 | 同 I-01 |
| **I-03** | `issues.edited`（有在跑任务） | Supersede 旧任务 | 旧 task 状态 = `cancelled`，`cancel_reason = superseded`；新 task 入队；stream 指针更新 |
| **I-04** | `issues.closed` | 取消当前任务，不投新任务 | 旧 task `cancelled`，`cancel_reason = issue_closed`；stream 指针删除；队列无新增 |
| **I-05** | `issues.reopened` | 触发新任务，不复活旧任务 | 新 task 入队；历史 cancelled task 状态不变 |
| **I-06** | `issues.deleted` | 取消任务 + 审计清理 | task `cancelled`；Postgres 中该 issue 记录被标记删除 |
| **I-07** | `issues.transferred` | 取消当前 stream，不自动接管 | 旧 task `cancelled`，`cancel_reason = transferred`；目标仓库不触发新 task |
| **I-08** | `issues.labeled` with `cancel-openbot` | 取消任务 | task `cancelled`，`cancel_reason = label_cancel` |
| **I-09** | `issues.labeled` with 其他标签（如 `bug`） | 忽略 | Redis 零变化；队列无新增 |
| **I-10** | `issues.unlabeled` | 任何情况忽略 | Redis 零变化 |
| **I-11** | `issues.assigned / unassigned` | 忽略 | Redis 零变化 |
| **I-12** | `issues.locked` | 取消 mention 类任务，不中断 triage | 进行中的 triage 不受影响；后续 mention 不被处理 |

**设计要点**：I-09 ~ I-12 都是"IGNORE"意图，L1 测试的重点是**意图分类器不会误判**，而不是测 Redis 状态（Redis 本就没变化）。

---

### B. 并发与时序（L2/L3）

核心思路：这一组测试的本质是验证**状态机在竞态条件下的一致性**，需要真实异步环境（L3）或精心设计的 mock 时序（L2）。

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **I-20** | 同一 issue 连续 5 个 edited 事件（快速重复） | 只有最后一个任务跑完 | 最终只有 1 个 `done` task；其余 4 个 `cancelled`，`cancel_reason = superseded` |
| **I-21** | Worker 跑到中途检测到 supersede | Worker 优雅退出，不跑完整 workflow | `TaskSupersededError` 被抛出；已消耗 token < 完整任务的 50% |
| **I-22** | Edited 事件的 `issue.updated_at` 早于 Redis 记录 | 丢弃过期事件 | Redis 零变化；返回 200 但无副作用 |
| **I-23** | 同一 `X-GitHub-Delivery` 重复投递 | 幂等去重 | 第二次请求直接 200，Redis 无任何变化 |
| **I-24** | webhook 写 Redis → 投队列的顺序保证 | Worker 拉到 task 时 stream 指针必须已建立 | Worker 拉到 task_id 时，`stream:current_task` 已存在（不会读到 None） |
| **I-25** | Issue close 发生在 worker ack 之后、第一个节点之前 | Pickup check 拦截退出 | Worker 不进入业务逻辑；task `cancelled` |
| **I-26** | Issue close 发生在 worker 执行工具调用中 | 工具调用结束后 cancel_check 拦截 | 不强杀进程；工具调用完成后检测到取消信号并优雅退出 |

**设计要点**：I-24 是写顺序保证，测的是"原子写" — 先写 Redis stream 指针，再投队列。如果顺序反了，worker 会先拿到 task_id，再查 stream 指针却发现 None，产生竞态。这是 L3 最重要的一个测试。

---

### C. 边界与异常（L1/L2）

核心思路：错误处理路径不能静默失败，每种错误都应有明确的响应码和日志，并且不能留下不一致的中间状态。

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **I-30** | Webhook 签名错误 | 401 响应，拒绝处理 | Redis 零变化；不投队列；不返回 payload 内容（避免信息泄露） |
| **I-31** | Webhook payload 缺字段（如无 `issue.number`） | 400 响应，拒绝处理 | 不进入业务逻辑；记录结构化错误日志 |
| **I-32** | Redis 临时不可用 | 5xx 响应，让 GitHub 重试 | 不消费事件；不写入部分状态；保证最终由 GitHub 重发 |
| **I-33** | Worker 崩溃后重启 | 未 ack 的任务能被其他 worker 认领 | Streams pending list 中有该 task；新 worker 拉到后继续或重跑 |
| **I-34** | Worker 写结果状态时 Redis 异常 | 任务进入 retry 或 `needs_review`，不静默丢失 | task 进入可观测的错误状态；不留在 `running` 假死 |

---

## 三、PR 场景测试

### A. PR 生命周期（L1/L2）

核心思路：PR review 的触发逻辑和 Issue triage 类似，但多了 check run 状态同步和 draft 判断。

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **P-01** | `pull_request.opened`（正式 PR） | 触发 review workflow | review stream 指针建立；check run 创建为 `in_progress` |
| **P-02** | `pull_request.synchronize`（推新 commit） | Supersede 旧 review | 旧 review `cancelled`，`cancel_reason = new_commit`；新 task 入队；旧 check run 更新为 `cancelled` |
| **P-03** | `pull_request.reopened` | 触发新 review | 同 P-01 |
| **P-04** | `pull_request.closed`（含 merged） | 取消正在跑的 review | task `cancelled`；check run 标记 `cancelled` 或 `neutral` |
| **P-05** | `pull_request.edited`（仅改标题/body） | 不触发新 review | Redis 零变化 |
| **P-06** | `pull_request.ready_for_review`（draft 转正式） | 触发第一次 review | draft 期间未触发过任何 review |
| **P-07** | Draft PR（`draft: true`） | 不触发 review workflow | 入口直接 skip，task 不入队（或入队后标记 skip） |

---

### B. 多 Commit / Synchronize 细节（L2/L3）

**背景**：`pull_request.synchronize` 是触发 review 的主要事件。每次 `git push` 是一个事件，不管推了多少个 commit。以下测试覆盖因多次 push 导致的各种竞态和语义问题。

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **P-50** | 一次 `git push` 含多个 commit | 只产生一个 review task | webhook 只收到一次事件；队列只入一个 task |
| **P-51** | 短时间内连续两次 push | 只有最后一次 review 跑完 | task_A `cancelled`，`cancel_reason = new_commit`；task_B `done` |
| **P-52** | Review 跑到一半时 push 新 commit | 旧 review 退出，新 review 基于最新 commit 重跑 | 旧 task 通过 cancel_check 退出；新 task `head_sha` = 新 commit；review 评论关联正确行号 |
| **P-53** | Force-push（history rewrite） | 旧 review 评论全部识别为 outdated，新 review 做完整 review | 通过 `before_sha` 和 `after_sha` 判断非 fast-forward；不复用 incremental 结果 |
| **P-54** | Force-push 后旧行内 review comment | Bot 不主动 resolve 旧评论 | 旧评论行号已不可信；bot 不做"已修复"判断 |
| **P-55** | Base branch 更新导致的 synchronize（head_sha 不变） | 不重跑 review | 检测 `head.sha` 与上次 review 的 sha 相同 → 跳过；只更新 `mergeable` 状态 |
| **P-56** | Worker 跑到一半时 PR 又被 push | Worker 基于 task 记录的 `head_sha` 工作，不 refetch | Worker 不重新 fetch latest sha；基于 task payload 中记录的 sha 完成当前 review |
| **P-57** | Review 评论提交时使用 task 的 `commit_id` | GitHub 评论绑定到正确 commit | GitHub review API 传 `commit_id` 参数；评论出现在正确的 commit 上 |

---

### C. Incremental Review（增量评审）（L2/L3）

**背景**：如果做增量评审，每次 review 只看本次 push 新增的 diff（`before..after`），而非整个 PR 的累积 diff（`base..head`）。

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **P-60** | 连续多次 synchronize，增量模式 | 每次 review 只覆盖新增 diff | task payload 中 diff 范围 = `previous_head..new_head` |
| **P-61** | 第一次 review（无历史） | 做全量 diff | task payload 标记 `is_incremental: false` |
| **P-62** | Force-push 后下一次 review | 退回全量模式 | 检测到 history rewrite 后，重置 incremental baseline |
| **P-63** | Base branch 更新（head 未变） | 不触发增量 review | head_sha 不变时跳过；不生成新 task |
| **P-64** | 增量 review 不重复评论历史问题 | 新 review 不重复前次评论 | 用 `(file, line, rule_id)` 去重；前一轮评论记录可查 |

---

### D. Fix Workflow（L2/L3）

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **P-10** | Fix workflow 产出 PR 后绑定原 issue | PR body 含 `Fixes #N`；issue stream 状态更新 | PR description 中有 issue 引用；`fix_pr_opened` 状态可查 |
| **P-11** | Self-fix ≤3 轮后仍失败 | 标记任务为 `failed`，不无限循环 | `task status = failed`，`reason = max_self_fix_exceeded`；PR 不提交或提交为 draft |
| **P-12** | Fix workflow 在 patch 已生成 + pytest 通过时被 supersede | Commit 点之后不再 cancel_check，PR 正常提交 | PR 已提交；新 task 在 issue 上重新开始 |
| **P-13** | 同一 issue 不同时存在两个 in_progress fix task | 检测到活跃 fix task 时拒绝或 supersede | 第二次触发 fix 时，第一个 task 被 supersede 或请求被拒绝 |

---

### E. Fork PR 安全约束（L1/L2）

核心思路：Fork PR 的核心安全边界是**代码执行隔离**，任何涉及 sandbox 的操作都不应在 fork PR 上运行。

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **P-20** | Fork PR 触发 review | Review 只读 diff，不执行代码 | `ForkPRGateMiddleware` 拦截 sandbox 调用；review 只做静态分析 |
| **P-21** | Fork PR 触发 fix workflow | 入口拒绝 | task `rejected`，`reason = fork_pr`；不进入 workflow |
| **P-22** | Fork PR 上的 @mention | 走 chat workflow，不走 fix | 只生成评论回复；工具集中无 sandbox/git 写操作 |
| **P-23** | Fork PR sandbox 环境变量 | 敏感 token 不出现在 sandbox 环境 | sandbox 启动配置中 `GITHUB_TOKEN` 等被剥离或降权 |

---

### F. Check Run 状态（L2/L3）

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **P-30** | Review 开始 | Check run 创建为 `in_progress` | GitHub API 收到 `POST /check-runs`，`status=in_progress` |
| **P-31** | Review 成功完成 | Check run 更新为 `success` | `conclusion=success`；`output` 含 review 摘要 |
| **P-32** | Review 被 supersede | 旧 check run 更新为 `cancelled` | 不留 `in_progress` 僵尸 check；`conclusion=cancelled` |
| **P-33** | Review 异常失败 | Check run 为 `failure`，含可读错误 | `conclusion=failure`；`output.summary` 含可读错误，不泄露 stack trace |
| **P-80** | 多 commit PR 的 check run | 每个 commit 上显示正确状态 | `head_sha` 正确绑定；不把多个 check run 全挂到同一个 commit |

---

## 四、Mention 场景测试

### A. 触发识别（L1）

核心思路：@mention 识别是纯文本解析，应该完全在 L1 用单元测试覆盖，测试各种边界 case。

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **M-01** | Issue 评论中 `@openbot` | 触发 chat workflow | chat stream 建立；triage stream 不受影响 |
| **M-02** | PR 顶层评论中 `@openbot` | 触发 chat，带 PR 上下文 | task payload 含 PR diff 引用 |
| **M-03** | PR 行内 review comment 中 `@openbot` | 触发 chat，带行号上下文 | task payload 含文件路径 + 行号 + diff hunk |
| **M-04** | `@openbot[bot]` 写法 | 等同 `@openbot` | 识别为相同触发 |
| **M-05** | `@OpenBot` 大小写变体 | 大小写不敏感 | 等同 `@openbot` |
| **M-06** | `email@openbot.com` | 不被识别为 mention | 不触发任何 task |
| **M-07** | 代码块内的 `@openbot`（` ``` ` 围栏内） | 不触发 | 不入队（避免讨论 bot 时被反复触发） |
| **M-08** | Quoted reply 中的 `@openbot`（`>` 开头） | 不触发 | 不入队 |

---

### B. 命令式 Mention（L2）

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **M-10** | `@openbot review` on PR | 路由到 review workflow，不走 chat | task type = review |
| **M-11** | `@openbot fix` on issue | 路由到 fix workflow | issue 需满足前置条件（已完成 triage） |
| **M-12** | `@openbot rerun` | 重新触发上一个同类任务 | 新 task 入队；旧 task 若在跑则 supersede |
| **M-13** | `@openbot stop` | 取消当前 stream 的任务 | task `cancelled`，`reason = mention_stop`；stream 指针清空 |
| **M-14** | `@openbot help` | 返回帮助评论，不跑任何 workflow | bot 评论包含命令列表；无 task 入队 |
| **M-15** | 未知命令 `@openbot foobar` | 友好降级到 chat | 走 chat workflow，由 LLM 理解意图 |

---

### C. Chat Stream 隔离（L2/L3）

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **M-20** | Triage 在跑时 @openbot 提问 | 两个 stream 互不影响 | triage task 不被取消；chat task 独立入队 |
| **M-21** | 同一 issue 连续两次 mention | 第二次 chat 能读到第一次的对话历史 | task payload 中携带对话历史引用 |
| **M-22** | 5s 内连续 3 个 mention | 只有最后一个跑完 | 前两个 chat task `cancelled`，`reason = superseded` |
| **M-23** | Issue close 时 chat stream 也被取消 | 所有 stream 统一处理 | chat task `cancelled`，`reason = issue_closed` |

---

### D. 权限与安全（L1/L2）

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **M-30** | 非 collaborator 用户 mention | 默认拒绝（可配置） | task 不入队；bot 回复"unauthorized"或静默忽略 |
| **M-31** | Bot 自己评论里的 `@openbot` | 不触发（防自激励） | sender.login == bot 时跳过 |
| **M-32** | Mention 含 prompt injection 内容 | 不影响系统 prompt | 用户内容包在隔离 tag 内；system prompt 不被覆盖 |
| **M-33** | Fork PR 上的 mention | 走 chat，不访问敏感工具 | chat 工具集白名单中无 sandbox/git 写操作 |

---

### E. 评论反馈循环（L2/L3）

| 测试 ID | 场景 | 测试目标 | 验收标准 |
|---------|------|----------|----------|
| **M-40** | Bot 回复后用户追问 | Bot 能识别上下文 | 新 chat task payload 包含完整评论历史 |
| **M-41** | 长任务的占位评论 | Task 开始 5s 内发"working on it..."；完成后编辑该评论 | 不新发评论；编辑而非追评 |
| **M-42** | 任务被 supersede 时占位评论 | 更新为"superseded by newer request" | 不留"working on it..."僵尸评论 |
| **M-43** | 任务失败时评论 | 含可读错误 + 重试入口 | 评论 footer 有 `@openbot rerun` 提示 |

---

## 五、横切关注点（Cross-Cutting）

以下测试与场景无关，属于系统级的健壮性保证，每一条都需要 L2/L3 覆盖。

| 测试 ID | 目标 | 验收标准 |
|---------|------|----------|
| **X-01** | 所有 webhook 在 100ms 内返回 200 | p99 latency < 100ms；不阻塞在 LLM/GitHub 调用上 |
| **X-02** | 所有 task 有 budget cap | 超 token/时间限制时抛 `BudgetExceeded`，优雅中止 |
| **X-03** | 所有 cancellation 路径都更新 task status | 没有 task 停留在 `running` 但实际已死 |
| **X-04** | 所有 stream 指针有 TTL | TTL > max_wall_clock；足够长但有上界，防止永久残留 |
| **X-05** | Dashboard API 能查任意 task 的完整生命周期 | 状态变迁、cancel_reason、token 消耗、耗时全部可见 |
| **X-06** | LangSmith trace 完整覆盖 workflow 每步 | 每个 task 一条 trace；含输入输出、tool calls、duration |
| **X-07** | 同一 task 的所有 GitHub API 调用共用 installation token | 缓存命中率 > 90%；不为每个 API call 重新换 token |

---

## 六、测试覆盖矩阵

| 场景 | L1 | L2 | L3 | L4 |
|------|----|----|----|----|
| Issue 单事件 (I-01~I-12) | ✓ intent 分类 | ✓ Redis 状态变化 | — | ✓ 冒烟 |
| Issue 并发时序 (I-20~I-26) | — | ✓ mock 时序 | ✓ 竞态验证 | — |
| Issue 边界异常 (I-30~I-34) | ✓ 签名、字段验证 | ✓ 响应码 | ✓ Redis 故障 | — |
| PR 生命周期 (P-01~P-07) | ✓ draft 判断 | ✓ check run | — | ✓ 冒烟 |
| 多 Commit (P-50~P-57, P-80) | ✓ SHA 比较逻辑 | ✓ 竞态模拟 | ✓ 并发 push | ✓ 实测 |
| Incremental Review (P-60~P-64) | ✓ diff 范围计算 | ✓ 去重逻辑 | — | — |
| Fix Workflow (P-10~P-13) | — | ✓ 状态机 | ✓ PR 创建 | — |
| Fork PR 安全 (P-20~P-23) | ✓ gate 逻辑 | ✓ 工具集白名单 | — | — |
| Check Run (P-30~P-33) | — | ✓ GitHub API mock | — | ✓ 实测 |
| Mention 识别 (M-01~M-08) | ✓ 文本解析 | — | — | — |
| 命令路由 (M-10~M-15) | ✓ 命令解析 | ✓ 路由决策 | — | — |
| Chat 隔离 (M-20~M-23) | — | ✓ stream 独立性 | ✓ 并发 | — |
| 权限安全 (M-30~M-33) | ✓ ACL 逻辑 | ✓ 拒绝路径 | — | — |
| 评论反馈 (M-40~M-43) | — | ✓ 评论状态 | — | ✓ 人工验证 |
| 横切目标 (X-01~X-07) | — | ✓ latency | ✓ budget/TTL | ✓ trace 验证 |

---

## 七、测试优先级与实施顺序

### Phase 1（v0.1 MVP，必须在 webhook-worker 上线前完成）

重点保障基本路径正确，防止明显的状态污染。

1. **I-01~I-05**: Issue 基本生命周期
2. **I-23**: 幂等去重（GitHub 会重发）
3. **I-30~I-32**: 错误响应（签名、字段、Redis 故障）
4. **P-01~P-04**: PR 基本生命周期
5. **P-20~P-23**: Fork PR 安全门
6. **M-31**: Bot 自激励防护
7. **X-01**: Webhook latency（必须不阻塞）
8. **X-03**: Cancellation 路径完整性

### Phase 2（v0.1 稳定后，并发场景）

1. **I-20~I-26**: Issue 并发时序
2. **P-51~P-55**: 多 commit 竞态
3. **M-20~M-23**: Chat stream 隔离
4. **X-02, X-04**: Budget cap + TTL

### Phase 3（v0.2，增量 review 和完整观测性）

1. **P-60~P-64**: Incremental review
2. **P-10~P-13**: Fix workflow 细节
3. **M-40~M-43**: 评论反馈循环
4. **X-05~X-07**: 观测性（dashboard、LangSmith、token 缓存）

---

## 八、测试文件组织建议

```
tests/
├── unit/
│   ├── test_intent_classifier.py     # I-01~I-12 的意图分类（L1）
│   ├── test_mention_parser.py        # M-01~M-08 的 mention 识别（L1）
│   ├── test_fork_pr_gate.py          # P-20 gate 逻辑（L1）
│   └── test_sync_sha_logic.py        # P-53/P-55 SHA 比较（L1）
├── state_machine/
│   ├── test_issue_lifecycle.py       # I-01~I-12 状态机（L2）
│   ├── test_issue_concurrency.py     # I-20~I-26 时序（L2）
│   ├── test_pr_lifecycle.py          # P-01~P-07, P-30~P-33（L2）
│   ├── test_multi_commit.py          # P-50~P-57（L2）
│   └── test_chat_stream.py           # M-20~M-23（L2）
├── integration/
│   ├── test_redis_ordering.py        # I-24 写顺序保证（L3）
│   ├── test_concurrent_supersede.py  # I-20, P-51 并发竞态（L3）
│   └── test_worker_recovery.py       # I-33 crash 恢复（L3）
└── e2e/
    └── README.md                     # L4 手动测试步骤（smee.io + 真实 GitHub）
```

---

## 附录：关键术语

| 术语 | 含义 |
|------|------|
| **stream** | 每个 issue/PR 在 Redis 中维护的状态指针，记录 `current_task_id` |
| **supersede** | 新事件导致旧 task 被取代，旧 task 状态 = `cancelled`，`reason = superseded` |
| **cancel_check** | Worker 在 workflow 节点间主动检查自己是否已被 supersede 的检查点 |
| **pickup check** | Worker 在处理第一个业务节点前的快速取消检查（比 cancel_check 更早） |
| **head_sha** | PR 当前最新 commit 的 SHA，用于绑定 review 评论和 check run |
| **intent** | Webhook 事件的语义意图，枚举值：`START` / `SUPERSEDE` / `CANCEL` / `IGNORE` |
| **is_incremental** | Review task 是否为增量模式（只看本次 push 的新 diff） |
