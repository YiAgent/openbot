# OpenBot 功能开发日志

> 整理日期：2026-05-22
> 组织方式：按大功能域分组；每个分组内部按开发顺序排列。省略纯依赖升级、merge commit 和无功能含义的格式修复。

---

## 1. 产品定义与项目基础

1. **项目初始化（2026-05-15）**  
   建立 OpenBot 仓库，加入 PRD、项目元数据、AGENTS/CLAUDE 配置和基础开发约束，确定“自托管 GitHub maintainer bot”的产品方向。

2. **PRD 与评测路线成型**  
   整理主 PRD、eval PRD、eval suites 文档和路线图，明确 v0.1 外部 benchmark、v0.2 internal dogfooding、v0.3 online 的质量演进路径。

3. **开发文档归档与清理**  
   将完成的 superpowers plans/specs、旧 webhook-worker 设计、架构重构文档和 eval 临时材料归档，保留当前设计作为主要入口。

4. **DeepAgents Runtime 设计**  
   写入 `DeepAgents Runtime` 正式 spec，目标是把公共 tools/middleware/checkpoint/sandbox/model 逻辑收敛到 base runtime，各任务 agent 只声明 profile。

---

## 2. 工程基础、CI 与本地开发体验

1. **CI 与本地质量门禁**  
   接入 GitHub Actions、pre-commit、ruff、trufflehog、Conventional Commit 检查，保证后续开发从一开始就有提交和安全扫描约束。

2. **FastAPI + uv 开发环境**  
   初始化 Python/uv/FastAPI 工程，加入 Makefile、setup wizard、docker-compose 和本地开发入口，让项目从文档进入可运行骨架。

3. **Doppler secrets infrastructure**  
   加入 Doppler 配置，为本地和部署环境统一管理 GitHub、Redis、Postgres、LLM 等敏感配置。

4. **本地开发命令与部署脚本校验**  
   持续整理 Makefile、Procfile、docker-compose、runbook 和路径引用，让本地开发、CI 和部署入口保持一致。

---

## 3. GitHub 集成与入口层

1. **GitHub ChannelAdapter 与 webhook 验证**  
   添加 `ChannelAdapter` 抽象和 `GitHubAdapter`，实现 webhook HMAC 验签、事件解析、回复、label 操作和 actor role 查询。

2. **GitHub App write-back**  
   实现 reply、add_label、remove_label、get_actor_role 等写回能力，让 bot 不只是接收事件，也能在 GitHub 上反馈结果。

3. **GitHub API 稳定性**  
   用 `gidgethub.sansio.validate_event` 替换手写 HMAC，并用 tenacity 重试 GitHub transient 5xx/connect 错误。

4. **GitHub Check Runs 实时反馈**  
   PR 事件创建 Check Run，worker 完成后更新状态，让用户在 GitHub UI 里看到 OpenBot 分析进度。

5. **Bot mention 兼容**  
   支持真实 bot handle `@yibots`，同时保留 `@openbot` 兼容，避免文档、测试和生产 App 名称不一致。

---

## 4. Workflow 输入链路与状态机

1. **基础 workflow ACK 与 LLM router**  
   加入 triage ACK、基础 LLM 路由和 dev loop，先跑通“GitHub 事件进来，OpenBot 能做出响应”的最小闭环。

2. **Harness Slice A：Router + Preflight + workflow stubs**  
   建立 `UnifiedEvent -> Dispatch -> handler` 的输入框架，加入 preflight runner 和 triage/review/fix/chat stub。

3. **Harness Slice B：取消、预算、限流中间件**  
   加入 cancel、budget、rate-limit middleware，把 PRD 里的安全/成本防护从设计落到输入链路。

4. **Harness Slice C：安全门禁与 chat parser**  
   加入 fork PR gate、actor role gate、prompt injection 包裹、输入 sanitize 和 `@openbot` chat parser。

5. **生产中间件 G1-G4**  
   输入侧补齐 sanitize、audit、feature toggle 和 dispatch chain order，让 webhook 进入 workflow 前的决策链更完整。

6. **Dispatcher direct actions、D10 classifier、incremental review**  
   加入直接动作短路、LLM intent classifier、增量 review 的 SHA 追踪和 `stages_to_run`，减少无意义 agent 执行。

7. **任务状态机与 supersede/cancel 语义**  
   通过 `run_id`、`prev_run_id`、`resource_key`、`event_seq` 区分 start/supersede/cancel/ignore，为并发 webhook 和长任务取消提供稳定身份。

---

## 5. Queue、Worker 与异步执行

1. **Redis webhook 去重**  
   用 Redis `SET NX EX` 做 `delivery_id` 去重，避免 GitHub 重试或重复 webhook 导致重复执行。

2. **Redis Stream worker queue**  
   用 Redis Stream worker 替换 FastAPI BackgroundTasks，形成 webhook 立即 202、后台 worker 异步执行的生产形态。

3. **Webhook-worker Layering F1**  
   加入 `TaskSpec v3`、`decide_and_enqueue` 和 worker v3 routing，把“webhook 决策”和“worker 执行”拆成更清楚的两段。

4. **QueuePayload v1/v2 兼容与 v3 过渡**  
   worker 同时支持旧 `QueuePayload` 和新 `TaskSpec v3`，保证滚动迁移时不会丢失队列中已有任务。

5. **Queue simplification 设计**  
   写入队列简化设计，目标是删除 BackgroundTask fallback 和旧 v1/v2 `QueuePayload` 兼容，收敛到单一 `TaskSpec` 路径。

6. **BackgroundTask fallback 移除**  
   从 webhook ingest/route 删除 BackgroundTask fallback，强化 Redis queue 作为生产唯一执行入口。

---

## 6. 数据持久化、审计、预算与配置

1. **Postgres 审计与成本表**  
   添加 `cost_meter`、`audit_log` 和 LiteLLM `complete()`，为预算控制、审计和后续 eval/trace 打基础。

2. **预算与限流查询接入 middleware**  
   中间件开始基于 Redis 和 Postgres 判断 rate-limit、per-task budget、monthly cap 等输入侧限制。

3. **配置加载与 feature toggle**  
   通过 repo 内 `.openbot/config.yaml` 和 `EffectiveConfig` 控制功能开关、阈值、预算和安全策略。

4. **环境变量健壮性**  
   对可选非字符串 env 做空字符串转 `None`，避免 Heroku/CI 把空 env 解析成非法 int/path 导致启动失败。

---

## 7. DeepAgents 生产工作流

1. **Chat DeepAgents adapter**  
   将 freeform `@openbot` chat 从纯 ACK 推进到 DeepAgents-backed responder；早期版本无工具，只根据事件上下文直接回答。

2. **Review DeepAgents responder**  
   落地 `DeepAgentsReviewResponder`，读取 PR diff，必要时通过 read-only GitHub tools 补上下文，并输出结构化 `ReviewFindings`。

3. **Fix DeepAgents responder**  
   落地 `DeepAgentsFixResponder`，在 sandbox 里读文件、写 patch、跑测试、取 `git_diff`，最终输出结构化 `FixOutcome`。

4. **Review/Fix use case 接入真实 responder**  
   review use case 将 findings 过滤后提交 PR Review；fix use case 根据 tests_passed 决定开 PR 还是评论测试失败。

5. **DeepAgents responder checkpoint 参数**  
   DeepAgents fix/review/chat responders 开始接受 `run_id + checkpointer`，为 LangGraph 中断恢复打通 agent 层接口。

6. **Fix workflow checkpoint/cancellation 收尾**  
   `fix.py` 继续补齐 `checkpointer + run_id` 传递、取消检查点和成功后的 `adelete_thread` 清理，让长 fix loop 更接近可恢复执行。

---

## 8. Sandbox 与代码执行环境

1. **SandboxPort 与 Daytona production adapter**  
   定义生产 fix loop 的 `SandboxPort`，实现 Daytona adapter，支持 clone、read/write/list、run、git diff、commit_and_push。

2. **Unified Sandbox Entry 设计与实现**  
   设计并实现统一 sandbox 入口：`CheckoutSpec`、`SandboxedHandle`、`SandboxPolicy`、`resolve_checkout`、dispatcher 统一 provision sandbox。

3. **Fix workflow 消费 `ctx.sandbox_handle`**  
   fix 从“handler 内部 clone”改成消费 dispatcher 预先 provision 的 `SandboxedHandle`，让 sandbox 生命周期由入口层统一管理。

4. **Sandbox bypass 与观测指标**  
   根据静态策略和 classifier 输出决定是否跳过 sandbox，并记录 sandbox bypass/cache metrics，方便区分正常跳过和降级失败。

5. **Sandbox snapshot cache 设计与第一批实现**  
   设计 Daytona snapshot cache，并实现 `SandboxCachePort`、cache key、NoOp backend、InMemory LRU+TTL backend，为降低 sandbox 冷启动成本铺路。

---

## 9. Checkpoint、取消与可恢复执行

1. **Agent checkpoint 设计**  
   设计 LangGraph Postgres checkpointer，用 `run_id` 作为 `thread_id`，让 worker 崩溃后的 agent run 可以从 checkpoint 恢复。

2. **agent_checkpointer 基础设施**  
   加入 `langgraph-checkpoint-postgres` 和 `agent_checkpointer` context manager，由 worker 生命周期持有 Postgres saver。

3. **Checkpoint 穿透 worker/context**  
   `PreflightContext`、`execute_handler`、`consume_loop`、worker `__main__` 开始携带 `agent_checkpointer` 到业务 handler。

4. **业务取消检查点设计**  
   设计在 fix/review/chat 的慢操作前后调用 cancellation checkpoint，避免用户取消后 agent 继续跑完整个长任务。

5. **Fix 取消与 checkpoint 清理落地**  
   fix 路径加入取消检查、agent checkpoint 参数传递和终态清理，优先覆盖耗时最长、成本最高的工作流。

---

## 10. Eval、Benchmark 与质量体系

1. **Eval 基础设施 E0/E1**  
   搭建 `evals/`、Inspect AI、LangSmith、预算校验、评测 governance 和基础测试，确定 OpenBot 要用外部 benchmark 衡量质量。

2. **DeepAgents eval baseline**  
   引入 DeepAgents review solver 和 Martian CodeReviewBench smoke，开始用真实 agent 跑公共 review benchmark。

3. **Prompt injection redteam eval**  
   加入 redteam prompt-injection 数据流和 failure category gate，用评测保护安全策略。

4. **Eval 数据集、scorer 与报告整理**  
   锁定 Martian benchmark、加入 SWE-bench 风格 patch scorer、compare runs、阈值模块，并清理旧 eval 规划文档。

5. **DeepAgents eval 稳定性增强**  
   修复 Docker sandbox 泄漏、汇总所有 LLM call 的 provider usage，并加入 structured-output finalizer 强制 schema 输出。

6. **SWE/SWT/Modal/Daytona eval 沙箱**  
   支持远程 Docker、Modal grading、Daytona TTL 回收，扩展 fix/test 类 benchmark 的运行环境。

7. **Eval agents 解耦与清理**  
   将 eval agents 从 Inspect AI 依赖中解耦，合并散乱 utility，统一 LLM 配置到 `ANTHROPIC_*`，让 eval baseline 更稳定。

---

## 11. 架构重构与代码组织

1. **Hexagonal 架构重构**  
   将平铺结构迁移到 `domain/application/infrastructure/core/entrypoints`，并引入 ports-adapters 协议，明确业务层和基础设施层职责。

2. **Ports-adapters contracts 与测试镜像**  
   引入 channel、queue、dedup、audit、rate limiter、sandbox、runs repo 等 port protocols，并同步迁移测试结构。

3. **死代码与重复测试清理**  
   清理旧 audit CLI、重复 eval utilities、stale tests 和无效 archive 内容，让主路径更容易导航。

4. **LLM 配置统一**  
   将模型配置统一到 `ANTHROPIC_*` 前缀，并将主要功能路由到 GLM-5.1 兼容 Anthropic 协议的模型路径。

---

## 12. 部署、监控与运维

1. **Heroku 部署与生产配置**  
   加入 Heroku web/worker 形态、inline PEM env、Redis timeout 调整和生产部署文档。

2. **Sentry 与 observability**  
   接入 Sentry SDK、Sentry metrics shim、JSON logging 和 profiler，建立生产异常、性能和请求日志可观测性。

3. **HTTP request logging 与 `/ready`**  
   补齐应用层 request id、结构化 HTTP 日志和 dependency-aware readiness endpoint，让 Heroku/uptime monitor 有稳定探针。

4. **Heroku monitoring add-ons runbook**  
   将 Memetria Redis monitoring、Better Uptime 等附加服务写入 `app.json`、bootstrap script 和 Heroku runbook，避免只靠控制台手工配置。

5. **Sentry AI monitoring**  
   接入 Sentry OpenAI integration 和 conversation id 绑定，并将 prompt/response 捕获保留在显式 PII opt-in 后面。
