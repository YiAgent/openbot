# Agent 中断恢复：Checkpoint 机制设计

**日期：** 2026-05-21  
**状态：** 待实现  
**依赖：** 现有 PostgreSQL（已部署），现有 `run_id` 状态机

---

## 背景

当前 Agent（fix / review / chat）在运行过程中没有任何中断恢复能力：

- Worker 进程崩溃 → PEL 重试 → **从头开始**（重新克隆仓库、重跑所有 LLM 步骤）
- 用户发取消指令 → 信号写入 Redis → **Agent 不检查**，继续跑完

Fix 工作流最典型：克隆仓库 + LLM 迭代修复 + 运行测试，可能耗时 5~15 分钟。进程崩了从头重跑，代价极高。

---

## 两个独立机制

这份文档覆盖两个互补的机制，需要分别实现：

| 机制 | 解决什么问题 | 实现方式 |
|------|-------------|----------|
| **LangGraph Agent Checkpoint** | 进程崩溃后 Agent 从上次中断处续跑 | `langgraph-checkpoint-postgres` + `run_id` 作为 thread_id |
| **业务层取消检查点** | 用户发取消指令时，Agent 能在关键步骤停下来 | 在 fix.py / review.py 关键步骤调 `checkpoint()` |

---

## 机制一：LangGraph Agent Checkpoint

### 工作原理

`create_deep_agent` 返回的是 LangGraph `CompiledStateGraph`。LangGraph 在每个 graph 节点执行完成后，把完整的 agent 状态（所有消息、tool call 结果、中间输出）持久化到 checkpointer。

```
agent.ainvoke(input, config={"configurable": {"thread_id": "xxx"}})

每个节点执行完：
    node_output → checkpointer.put(thread_id, checkpoint)

进程崩溃后，用同一个 thread_id 重新调用：
    agent.ainvoke(input, config={"configurable": {"thread_id": "xxx"}})
    → checkpointer.get(thread_id) → 找到最后一个检查点 → 从那里继续
```

**关键：** `thread_id` 相同 = LangGraph 认为是同一次对话，自动续跑。`thread_id` 不同 = 全新开始。

### thread_id 映射

使用状态机已经产出的 `run_id`：

```python
# run_id 由 runs_repo.transition() 产出，格式：
# SHA-256(resource_key + monotonic_ns)[:32]
# 例：a3f8c2d1e4b7091256ce3a87fb204d19

config = {
    "recursion_limit": _RECURSION_LIMIT,
    "configurable": {"thread_id": run_id},  # ← 新增
}
```

`run_id` 已经在 Worker 的 `_execute_task_spec` 里通过 `upgrade_dispatch` 注入到 `PreflightContext`，向下传到 handler，再传到 responder。

### 需要的包

```toml
# pyproject.toml
"langgraph-checkpoint-postgres>=2.0"   # 提供 AsyncPostgresSaver
```

`asyncpg` 已安装（0.31.0），是 `AsyncPostgresSaver` 的底层驱动，不需要额外安装。

### Postgres 表结构

`langgraph-checkpoint-postgres` 会在首次 `setup()` 时自动创建 4 张表：

```sql
checkpoints              -- 每个 thread 的最新 checkpoint 指针
checkpoint_blobs         -- 实际状态数据（消息、tool results 等），按 channel 分片
checkpoint_writes        -- 待写入的 pending writes（crash recovery 用）
checkpoint_migrations    -- 版本控制
```

这些表独立于现有的 `audit_log` / `cost_meter` / `task_runs`，不冲突。通过 Alembic migration 来管理（见实现步骤）。

### 代码改动

**新增：** `openbot/infrastructure/persistence/agent_checkpointer.py`

```python
"""LangGraph agent checkpointer — Postgres-backed, async."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


@asynccontextmanager
async def agent_checkpointer(dsn: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Yield a ready-to-use AsyncPostgresSaver.

    Caller is responsible for closing the connection pool.
    Used once per Worker lifetime, not per request.
    """
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()   # 幂等，创建表（已存在则跳过）
        yield saver
```

**修改：** `openbot/infrastructure/agents/deepagents_fix.py`

```python
# 新增参数
async def fix_for_event(
    self,
    event: UnifiedEvent,
    *,
    adapter: ChannelAdapterPort,
    sandbox: SandboxPort,
    issue: dict[str, Any],
    run_id: str | None = None,           # ← 新增
    checkpointer: BaseCheckpointSaver | None = None,  # ← 新增
) -> FixOutcome:
    ...
    agent = create_deep_agent(
        model=...,
        tools=tools,
        system_prompt=_SYSTEM_PROMPT,
        response_format=FixOutcomeSchema,
        checkpointer=checkpointer,        # ← 新增：None 时不持久化
    )
    config: dict = {"recursion_limit": _RECURSION_LIMIT}
    if run_id and checkpointer:
        config["configurable"] = {"thread_id": run_id}  # ← 新增

    result = await agent.ainvoke({"messages": [...]}, config=config)
    return _extract_outcome(result)
```

同样修改 `deepagents_review.py` 和 `deepagents_chat.py`。

**修改：** `openbot/application/use_cases/fix.py`

```python
# _generate_fix_outcome 新增透传 run_id + checkpointer
async def _generate_fix_outcome(
    *,
    sandbox: SandboxPort,
    event: UnifiedEvent,
    adapter: ChannelAdapterPort,
    issue: dict[str, Any],
    run_id: str | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> FixOutcome:
    responder = DeepAgentsFixResponder()
    return await responder.fix_for_event(
        event,
        adapter=adapter,
        sandbox=sandbox,
        issue=issue,
        run_id=run_id,
        checkpointer=checkpointer,
    )
```

**修改：** Worker 在启动时初始化 checkpointer，通过 `PreflightContext` 或直接注入传入 handler：

```python
# worker.py: consume_loop 启动时
async with agent_checkpointer(settings.database_url) as cp:
    ctx.agent_checkpointer = cp   # 或通过 DI 注入
    await consume_loop(redis, ...)
```

### Checkpoint 清理

Agent 成功完成后清理 checkpoint，避免 Postgres 无限增长：

```python
# 在 fix.py / review.py 成功路径的末尾
if run_id and checkpointer:
    await checkpointer.adelete_thread(run_id)
```

失败进入 DLQ 后：DLQ 处理时清理对应 run_id 的 checkpoint。

### 进程崩溃恢复流程

```
第一次执行（run_id = "a3f8c2d1"）
    Agent 跑了 8 个 tool call，第 9 个时进程崩溃
    Checkpoint 存有前 8 步的完整状态
    消息未 XACK → PEL

60 秒后，Worker XAUTOCLAIM
    重新执行，传入同一个 run_id = "a3f8c2d1"
    LangGraph 读到 checkpoint → 从第 9 步继续
    不重跑前 8 步，不重新克隆仓库（沙箱需另行处理，见注意事项）

执行完成 → XACK → 删除 checkpoint
```

### 注意事项：沙箱不能续跑

LangGraph checkpoint 能恢复 **消息历史和 LLM 状态**，但沙箱（Daytona）是独立的外部进程。进程重启后沙箱已销毁，克隆好的代码不在了。

处理方案：
- Agent 重启时，fix handler 先重新克隆，再让 Agent 接着跑
- Agent 的 LLM "记忆"（已识别的文件、已尝试的修改方向）通过 checkpoint 保留，不会从零开始分析
- 实际重跑的只有 `write_file` / `run_command` 等操作，LLM 推理步骤不重跑

---

## 机制二：业务层取消检查点

### 现状

`cancellation.checkpoint(redis, run_id)` 函数已实现，在 `debug_echo.py` 中有使用示例，但 fix / review / chat 三个真实 handler 没有调用。

### 在哪里加检查点

每个"慢操作"前后加一次检查，让取消信号能在合理的时间内生效：

**fix.py：**

```python
async def maybe_run_fix(ctx):
    run_id = ctx.dispatch.run_id

    issue = await adapter.get_issue(...)
    await checkpoint(ctx.redis, run_id)   # ① 读取 issue 后检查

    async with factory() as sandbox:
        await sandbox.clone(...)
        await checkpoint(ctx.redis, run_id)   # ② 克隆完成后检查

        outcome = await _generate_fix_outcome(...)
        await checkpoint(ctx.redis, run_id)   # ③ Agent 完成后检查
        # （Agent 内部也可以通过 interrupt_on 加检查，见下）

        await adapter.create_branch(...)
        await checkpoint(ctx.redis, run_id)   # ④ 建分支后检查

        await sandbox.commit_and_push(...)
        await checkpoint(ctx.redis, run_id)   # ⑤ 推送后检查

        await adapter.open_pull_request(...)
```

**review.py：**

```python
async def maybe_run_review(ctx):
    run_id = ctx.dispatch.run_id
    findings = await _generate_review_findings(...)
    await checkpoint(ctx.redis, run_id)   # ① LLM 审查完成后检查

    # 提交审查
    await ctx.adapter.create_pr_review(...)
```

**chat.py：**

```python
async def maybe_run_chat(ctx):
    run_id = ctx.dispatch.run_id
    # freeform 分支：LLM 调用前后各一次
    await checkpoint(ctx.redis, run_id)   # ① LLM 调用前
    response = await responder.reply_for_event(...)
    await checkpoint(ctx.redis, run_id)   # ② LLM 完成后
```

`checkpoint()` 内部会 raise `RunCancelledError`，现有 audit_lifecycle 会捕获并记录 CANCELLED。

### Agent 内部取消（进阶）

`create_deep_agent` 支持 `interrupt_on` 参数，可以在 graph 特定节点前注入用户确认或取消检查。但这需要更深的集成（需要 Human-in-the-loop 协议），留作后续 slice。

---

## 实现顺序

```
1. 安装 langgraph-checkpoint-postgres，编写 Alembic migration
2. 实现 agent_checkpointer context manager
3. 修改 deepagents_fix / review / chat 接受 checkpointer + run_id
4. 修改 fix.py / review.py / chat.py 的 use case 透传参数
5. Worker 启动时初始化 checkpointer，传入 execute_handler 链路
6. 在 fix.py / review.py / chat.py 加取消 checkpoint() 调用
7. 测试：用 MemorySaver 替代 AsyncPostgresSaver 跑单元测试
```

---

## 不在本次范围

- Agent 内部 `interrupt_on` 取消（需要 Human-in-the-loop 协议）
- Checkpoint 数据的加密存储（消息内容含代码和 issue 描述）
- 跨多个 run_id 的 checkpoint 聚合分析
