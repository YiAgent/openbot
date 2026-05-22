# 消息队列架构简化设计

**日期：** 2026-05-21  
**状态：** 待实现  
**背景：** 项目未上线，无需向后兼容，可以做破坏性清理

---

## 问题

当前实现有两个遗留设计，增加了不必要的复杂度：

### 问题 1：BackgroundTask 降级路径

Redis 写入失败时，`ingest_webhook` 会返回一个 `_BackgroundDispatch` 信号，FastAPI 路由层捕获后把 `decide_and_enqueue` 注册为 BackgroundTask，在 **web 进程内** 直接跑完分析和执行。

**为什么这是问题：**
- Web 进程的职责是快速处理 HTTP 请求，不应该跑 AI 任务
- 没有重试保障，进程重启任务丢失
- "Redis 不可用"是异常状态，不是需要优雅降级的正常情况
- GitHub webhook 本身有内置重试（指数退避，可重试数天），让它重试即可

### 问题 2：多版本消息格式兼容

Worker 的 `_process_entry` 需要兼容 v1 / v2 / v3 三种格式，路由到不同处理函数：
- v1/v2（旧格式）→ `run_dispatch`（preflight + handler 一体式）
- v3（新格式）→ `_execute_task_spec` → `execute_handler`

**为什么这是问题：**
- 项目未上线，不存在需要兼容的旧数据
- 两套处理逻辑并存，测试覆盖成本翻倍
- `run_dispatch` 存在的唯一原因是兼容旧格式，可以删除

---

## 目标设计

### 唯一路径：所有任务都经过 Worker

```
GitHub 推送
    │
    ▼
FastAPI（签名验证 · 去重 · 状态机 · PR 进度标记 · 写队列）
    │
    │  Redis 写入失败 → 直接返回 500，GitHub 自动重试
    ▼
Redis Stream（只有 v3 TaskSpec 格式）
    │
    ▼
Worker（decide_and_enqueue：preflight · AI 分类 · DiffScope）
    │
    │  preflight 不通过 → XACK，结束
    ▼
Worker（execute_handler：直接调业务 handler）
    │
    ▼
triage / review / fix / chat
```

### 两阶段还是一阶段？

**保留两阶段**（推荐）：
- `ingest_webhook` 写入精简的 TaskSpec（只含事件信息）
- Worker 读取后跑 `decide_and_enqueue`（分析阶段）
- 分析通过后写新 TaskSpec（含完整执行方案）
- Worker 再次读取，跑 `execute_handler`

**理由：**
- preflight 失败可以快速 XACK，不占用执行阶段的 Worker 资源
- 分析结果持久化在 TaskSpec 里，执行失败重试时不需要重跑 LLM 分类
- 架构意图清晰：分析和执行是不同职责

如果后续觉得两次队列读写延迟不可接受，再做合并也不复杂。

---

## 需要改动的文件

### 删除逻辑

| 文件 | 删除内容 |
|------|----------|
| `openbot/application/use_cases/ingest_webhook.py` | `_BackgroundDispatch` dataclass；`IngestResult.background_dispatch` 字段；Redis 失败时的 BackgroundTask 分支，改为直接 raise |
| `openbot/entrypoints/api/routes/github_webhook.py` | `if result.background_dispatch: add_task(...)` 这段逻辑 |
| `openbot/infrastructure/queue/worker.py` | `_process_entry` 中 v1/v2 的路由分支；v1/v2 对应的处理函数 |
| `openbot/application/dispatcher.py` | `run_dispatch`（只在 v2 路径和 BackgroundTask 中使用，两者都删后可以整体移除） |

### 修改逻辑

| 文件 | 修改内容 |
|------|----------|
| `openbot/application/use_cases/ingest_webhook.py` | Redis 写入失败时直接 `raise`，不返回任何降级信号 |
| `openbot/infrastructure/queue/worker.py` | `_process_entry` 只保留 v3 处理逻辑，移除版本路由 |

### 保留不动

- `decide_and_enqueue`（分析阶段入口，保留）
- `execute_handler`（执行阶段入口，保留）
- `build_preflight_chain`（10 道安全检查定义，保留）
- 所有业务 handler：`triage.py` / `review.py` / `fix.py` / `chat.py`

---

## 本地开发影响

移除降级路径后，本地开发**必须**启动 Redis，否则 webhook 会直接 500。

`docker-compose.yml` 中已有 Redis 服务，`make dev` 应同时启动。需要在 README 里更新：

```
本地开发前提：
  - Docker 运行中（Redis 通过 docker-compose 启动）
  - make dev 启动 FastAPI
  - make worker 启动 Worker 进程（新增 Makefile target）
```

---

## 需要删除的测试

- 所有针对 v1/v2 消息处理的 Worker 单元测试
- 所有针对 BackgroundTask 降级路径的集成测试
- `run_dispatch` 相关测试（如果 `run_dispatch` 整体删除）

现有 v3 路径的测试保留不动。

---

## 不在本次范围内

- 安全检查并发化（部分检查串行改并发）——独立 slice
- Worker 池拆分（分析 Worker 和执行 Worker 分开）——待流量验证后决定
- Redis 高可用（Cluster / Sentinel）——运维 slice
