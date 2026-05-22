# OpenBot 功能开发日志

> 整理日期：2026-05-22
> 来源：按项目 git 历史顺序梳理，省略纯依赖升级、merge commit 和无功能含义的格式修复。

1. **项目初始化（2026-05-15）**  
   建立 OpenBot 仓库，加入 PRD、项目元数据、AGENTS/CLAUDE 配置和基础开发约束，确定“自托管 GitHub maintainer bot”的产品方向。

2. **CI 与本地质量门禁**  
   接入 GitHub Actions、pre-commit、ruff、trufflehog、Conventional Commit 检查，保证后续开发从一开始就有提交和安全扫描约束。

3. **FastAPI + uv 开发环境**  
   初始化 Python/uv/FastAPI 工程，加入 Makefile、setup wizard、docker-compose 和本地开发入口，让项目从文档进入可运行骨架。

4. **GitHub ChannelAdapter 与 webhook 验证**  
   添加 `ChannelAdapter` 抽象和 `GitHubAdapter`，实现 webhook HMAC 验签、事件解析、回复、label 操作和 actor role 查询。

5. **基础 workflow ACK 与 LLM router**  
   加入 triage ACK、基础 LLM 路由和 dev loop，先跑通“GitHub 事件进来，OpenBot 能做出响应”的最小闭环。

6. **Redis webhook 去重**  
   用 Redis `SET NX EX` 做 `delivery_id` 去重，避免 GitHub 重试或重复 webhook 导致重复执行。

7. **Postgres 审计与成本表**  
   添加 `cost_meter`、`audit_log` 和 LiteLLM `complete()`，为预算控制、审计和后续 eval/trace 打基础。

8. **Eval 基础设施 E0/E1**  
   搭建 `evals/`、Inspect AI、LangSmith、预算校验、评测 governance 和基础测试，确定 OpenBot 要用外部 benchmark 衡量质量。

9. **Harness Slice A：Router + Preflight + workflow stubs**  
   建立 `UnifiedEvent -> Dispatch -> handler` 的输入框架，加入 preflight runner 和 triage/review/fix/chat stub。

10. **Harness Slice B：取消、预算、限流中间件**  
    加入 cancel、budget、rate-limit middleware，把 PRD 里的安全/成本防护从设计落到输入链路。

11. **Harness Slice C：安全门禁与 chat parser**  
    加入 fork PR gate、actor role gate、prompt injection 包裹、输入 sanitize 和 `@openbot` chat parser。

12. **DeepAgents eval baseline**  
    引入 DeepAgents review solver 和 Martian CodeReviewBench smoke，开始用真实 agent 跑公共 review benchmark。

13. **Prompt injection redteam eval**  
    加入 redteam prompt-injection 数据流和 failure category gate，用评测保护安全策略。

14. **Redis Stream worker queue**  
    用 Redis Stream worker 替换 FastAPI BackgroundTasks，形成 webhook 立即 202、后台 worker 异步执行的生产形态。

15. **Eval 数据集、scorer 与报告整理**  
    锁定 Martian benchmark、加入 SWE-bench 风格 patch scorer、compare runs、阈值模块，并清理旧 eval 规划文档。

16. **DeepAgents eval 稳定性增强**  
    修复 Docker sandbox 泄漏、汇总所有 LLM call 的 provider usage，并加入 structured-output finalizer 强制 schema 输出。

17. **SWE/SWT/Modal/Daytona eval 沙箱**  
    支持远程 Docker、Modal grading、Daytona TTL 回收，扩展 fix/test 类 benchmark 的运行环境。

18. **生产中间件 G1-G4**  
    输入侧补齐 sanitize、audit、feature toggle 和 dispatch chain order，让 webhook 进入 workflow 前的决策链更完整。

19. **Heroku 部署与生产配置**  
    加入 Heroku web/worker 形态、inline PEM env、Redis timeout 调整和生产部署文档。

20. **Sentry 与 observability**  
    接入 Sentry SDK、Sentry metrics shim、JSON logging 和 profiler，建立生产异常、性能和请求日志可观测性。

21. **GitHub API 稳定性**  
    用 `gidgethub.sansio.validate_event` 替换手写 HMAC，并用 tenacity 重试 GitHub transient 5xx/connect 错误。

22. **GitHub Check Runs 实时反馈**  
    PR 事件创建 Check Run，worker 完成后更新状态，让用户在 GitHub UI 里看到 OpenBot 分析进度。

23. **Bot mention 兼容**  
    支持真实 bot handle `@yibots`，同时保留 `@openbot` 兼容，避免文档、测试和生产 App 名称不一致。

24. **Hexagonal 架构重构**  
    将平铺结构迁移到 `domain/application/infrastructure/core/entrypoints`，并引入 ports-adapters 协议，明确业务层和基础设施层职责。

25. **Webhook-worker Layering F1**  
    加入 `TaskSpec v3`、`decide_and_enqueue` 和 worker v3 routing，把“webhook 决策”和“worker 执行”拆成更清楚的两段。

26. **Dispatcher direct actions、D10 classifier、incremental review**  
    加入直接动作短路、LLM intent classifier、增量 review 的 SHA 追踪和 `stages_to_run`，减少无意义 agent 执行。

27. **DeepAgent review/fix 生产工作流**  
    落地生产侧 `DeepAgentsReviewResponder` 和 `DeepAgentsFixResponder`，review 输出结构化 findings，fix 在 sandbox 里读写文件、跑测试并产出 `FixOutcome`。

28. **Eval agents 解耦与清理**  
    将 eval agents 从 Inspect AI 依赖中解耦，合并散乱 utility，统一 LLM 配置到 `ANTHROPIC_*`，让 eval baseline 更稳定。

29. **Unified Sandbox Entry 设计与实现**  
    设计并实现统一 sandbox 入口：`CheckoutSpec`、`SandboxedHandle`、`SandboxPolicy`、`resolve_checkout`、dispatcher 统一 provision sandbox，fix 改为消费 `ctx.sandbox_handle`。

30. **Sandbox bypass 与观测指标**  
    根据静态策略和 classifier 输出决定是否跳过 sandbox，并记录 sandbox bypass/cache metrics，方便区分正常跳过和降级失败。

31. **Queue simplification 设计**  
    写入队列简化设计，目标是删除 BackgroundTask fallback 和旧 v1/v2 `QueuePayload` 兼容，收敛到单一 `TaskSpec` 路径。

32. **Sandbox snapshot cache 设计与第一批实现**  
    设计 Daytona snapshot cache，并实现 `SandboxCachePort`、cache key、NoOp backend、InMemory LRU+TTL backend，为降低 sandbox 冷启动成本铺路。

33. **Agent checkpoint 设计与基础设施**  
    设计 LangGraph Postgres checkpointer 和业务取消检查点，并加入 `langgraph-checkpoint-postgres`、`agent_checkpointer` context manager。

34. **Checkpoint 穿透 worker/context/responder**  
    `PreflightContext`、`execute_handler`、`consume_loop`、worker `__main__`、DeepAgents fix/review/chat responders 开始接受 `run_id + checkpointer`。

35. **BackgroundTask fallback 移除**  
    从 webhook ingest/route 删除 BackgroundTask fallback，强化 Redis queue 作为生产唯一执行入口。

36. **DeepAgents Runtime 设计**  
    写入 `DeepAgents Runtime` 正式 spec，目标是把公共 tools/middleware/checkpoint/sandbox/model 逻辑收敛到 base runtime，各任务 agent 只声明 profile。

37. **Fix workflow checkpoint/cancellation 收尾**  
    `fix.py` 继续补齐 `checkpointer + run_id` 传递、取消检查点和成功后的 `adelete_thread` 清理，让长 fix loop 更接近可恢复执行。
