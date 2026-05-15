# Open SWE 项目深度教学文档

> 阅读对象：**已会 Python，但是第一次接触 LangGraph + Deep Agents + 沙箱型 AI 编码代理** 的开发者。
> 阅读这份文档后，你应该可以：① 看懂每个文件夹的职责；② 在脑海里画出一次完整调用链；③ 自己加新工具 / 新中间件 / 新沙箱后端。

---

## 0. 一句话概括项目

**Open SWE** 是 LangChain 开源的"团队内部编码 Agent 的脚手架"。它把 *LangGraph*（图编排）+ *Deep Agents*（深度代理框架）+ *远程云沙箱*（Modal/Daytona/LangSmith 等）+ *Slack / Linear / GitHub 三入口* 拼成了一个**接收事件 → 起沙箱 → 跑 LLM → 提 PR → 回消息**的闭环。

```
        ┌─────────────────────────────────────────────────────────┐
        │                       触发入口                            │
        │  Slack @open-swe   Linear @openswe   GitHub @open-swe     │
        └────────────┬──────────────┬────────────────┬─────────────┘
                     │              │                │
                     ▼              ▼                ▼
            ┌─────────────────────────────────────────────┐
            │  FastAPI (agent/webapp.py)                  │
            │  · 校验签名  · 解析 thread_id  · 排重         │
            │  · 拉取上下文(Issue/PR/Slack 历史)            │
            │  · 用 langgraph_sdk.client 触发图运行          │
            └────────────────────┬────────────────────────┘
                                 │
                                 ▼
            ┌─────────────────────────────────────────────┐
            │  LangGraph 图工厂 (agent/server.py:get_agent) │
            │  · 解析 GitHub Token   · 确保沙箱可用           │
            │  · 装中间件栈          · 装工具列表            │
            │  · 返回 deep_agent (Pregel)                  │
            └────────────────────┬────────────────────────┘
                                 │
                                 ▼
            ┌─────────────────────────────────────────────┐
            │  Deep Agent 主循环 (loop)                    │
            │  before_model → LLM → tools → after_model    │
            │  ┌─ 工具在沙箱里执行（execute / read_file…）   │
            │  └─ 工具也可调外部 API（gh, linear, slack）    │
            └─────────────────────────────────────────────┘
```

> **关键点 1**：Agent 本身**无状态**。所有"线程状态"（沙箱 ID、加密 token、reviewer findings）都存在 LangGraph 的 *thread metadata* 上。
> **关键点 2**：进程里有**两个图**——`agent`（写代码）和 `reviewer`（看 PR），它们共享同一套沙箱生命周期逻辑。

---

## 1. 项目顶层结构

```
open-swe/
├── agent/                # 全部业务代码（图工厂 + 工具 + 中间件 + 工具函数）
│   ├── server.py         # ★ "agent" 图：会写代码 + 提 PR 的主代理
│   ├── reviewer.py       # ★ "reviewer" 图：只读 PR + 写 findings 的代码审查代理
│   ├── webapp.py         # ★ FastAPI 应用，承载 /webhooks/{linear,slack,github}
│   ├── prompt.py         # System prompt 构造（拼装工作目录、AGENTS.md、人物信息等）
│   ├── encryption.py     # Fernet/MultiFernet 加密 / 解密 OAuth Token
│   ├── reviewer_diff.py  # PR diff 行号 → (file, line_set) 校验
│   ├── reviewer_findings.py  # Reviewer findings 的存储 schema + CRUD
│   ├── reviewer_publish.py   # publish_review 的底层：组装 inline 评论 + GraphQL
│   │
│   ├── tools/            # LLM 可调用的工具（≈ 18 个）
│   ├── middleware/       # 围绕模型/工具调用的钩子（≈ 9 个）
│   ├── utils/            # 与 GitHub/Slack/Linear/沙箱/Token 打交道的辅助库
│   └── integrations/     # 沙箱后端工厂：LangSmith / Modal / Daytona / Runloop / Local
│
├── tests/                # pytest 单测；middleware/ 子目录里是中间件场景化测试
├── evals/reviewer/       # Reviewer 的 LLM-as-judge 评测脚手架（数据集 + 评分器）
├── scripts/              # 运维脚本（建沙箱快照、查 PR merge 状态）
├── static/               # 项目 logo
├── langgraph.json        # LangGraph 服务清单：声明两个图 + FastAPI app
├── pyproject.toml        # uv 项目定义；ruff 行宽 100，py311
├── Makefile              # make install / dev / run / test / lint / format
└── CLAUDE.md / AGENTS.md / CUSTOMIZATION.md   # 给 AI 编码助手看的说明
```

后面每一节聚焦一个文件夹，**先讲它解决什么问题，再举一两个最关键的文件**。

---

## 2. `agent/` 顶层：两张图 + Web 入口

### 2.1 `langgraph.json` —— LangGraph 的服务清单

```json
{
  "python_version": "3.12",
  "graphs": {
    "agent":    "agent.server:get_agent",
    "reviewer": "agent.reviewer:get_reviewer_agent"
  },
  "http": { "app": "agent.webapp:app" },
  "env": ".env"
}
```

- LangGraph dev server 启动时，会按 `graphs` 配置把每个图函数注册成可远程触发的图。
- `get_agent(config)` 是**每次起线程都被调用一次的工厂函数**，不是单例。这就是为什么沙箱、Token、Prompt 都能根据当前 thread 自定义。
- `http.app` 是同进程内挂载的 FastAPI 应用，**和图共用一个 Python 进程**——这是 open-swe 区别于"webhook 服务 + 任务队列"两层架构的关键设计：webhook 用 `langgraph_sdk.client` 触发图运行，而图运行本身又在同一进程里。

### 2.2 `agent/server.py` —— "agent" 图工厂

文件不长（421 行），但它是全项目的"中枢"。核心读这五个函数就够：

| 函数 | 责任 |
|---|---|
| `get_agent(config)` | LangGraph 调用入口。组装 model + prompt + tools + middleware 并返回 `Pregel`。 |
| `ensure_sandbox_for_thread(thread_id)` | **四态沙箱生命周期管理**（见下）。 |
| `_create_sandbox_with_proxy()` | 调 `create_sandbox()` 起一个沙箱，再用 GitHub App token 配 LangSmith 代理。 |
| `_refresh_github_proxy(...)` | 复用沙箱时刷新代理里的 GitHub token，因为 installation token 1 小时过期。 |
| `_configure_git_identity(...)` | 在沙箱里设置 `git config user.name/email`，否则 Vercel preview 会拒掉提交。 |

#### 四态沙箱生命周期

每次图被调用，`ensure_sandbox_for_thread` 都会处理这四种情况：

| 状态 | 内存缓存 `SANDBOX_BACKENDS[tid]` | metadata `sandbox_id` | 处理动作 |
|---|---|---|---|
| 1 | ✅ 有 | 任意 | 用 `echo ok` ping，挂掉就重建 |
| 2 | ❌ 无 | `__creating__` 哨兵 | 轮询元数据直到 ID 写好（另一进程在建） |
| 3 | ❌ 无 | `None` | 起新沙箱，先写 `__creating__`，再写真实 ID |
| 4 | ❌ 无 | 已有真实 ID | 用 ID 重连；失败时回退到 3 |

> 这就是 CLAUDE.md 里反复强调的"sandbox lifecycle is the tricky part"。看不懂这段，调试 `langgraph dev` 经常会一脸懵。

#### 模型选型与回退

```python
DEFAULT_LLM_MODEL_ID = "openai:gpt-5.5"
DEFAULT_LLM_REASONING = {"effort": "medium"}
```

通过 `LLM_MODEL_ID` 环境变量可换主模型；`LLM_FALLBACK_MODEL_ID` 没显式设置时，`fallback_model_id_for()` 会按规则自动选另一家厂商（Anthropic↔OpenAI 双向回退），由 `ModelFallbackMiddleware` 在 5xx/429/超时时切换。

#### 中间件栈（顺序很重要！）

```python
middleware=[
    SanitizeToolInputsMiddleware(),                   # 1. 修正 LLM 错填的 int 参数
    ModelCallLimitMiddleware(run_limit=5000),         # 2. 步数上限保护
    ToolErrorMiddleware(),                            # 3. 工具异常 → ToolMessage，不崩溃
    check_message_queue_before_model,                 # 4. 把"运行中收到的新消息"塞进上下文
    SlackAssistantStatusMiddleware(),                 # 5. Slack 输入框 "AI is working..." 保活
    ensure_no_empty_msg,                              # 6. 不允许空 AI 消息，避免 Anthropic 拒收
    notify_step_limit_reached,                        # 7. after-agent：触顶时主动发 Slack
    SandboxCircuitBreakerMiddleware(),                # 8. 同一沙箱连挂两次就放弃
    *fallback_middleware,                             # 9. 5xx → 切到备用模型
]
```

> 别小看这个顺序：把 `SanitizeToolInputsMiddleware` 放在最前，是因为它要在 Pydantic 校验之前把参数从字符串改成 int。把 `notify_step_limit_reached` 放在 `ensure_no_empty_msg` 之后，是因为它依赖最后一条消息的内容里有 `"Model call limits exceeded"` 标记字符串。

### 2.3 `agent/reviewer.py` —— "reviewer" 图工厂

和 `server.py` 一个套路，但只装四个工具：`add_finding / update_finding / list_findings / publish_review`。**System Prompt 内嵌了完整的审查 SOP**：

- 一定先 `gh pr diff` 自取 diff（再 review 时改用 `gh api compare` 增量取）。
- **只评 diff 内的行**，不允许评 PR 没动过的代码。
- 严重程度分 5 档（informational/low/medium/high/critical），自己校准，禁止灌水。
- `suggestion` 字段只能塞 ≤4 行的小改，超过自动丢弃——这是为了**不让 review comment 变成"reviewer 替你重写文件"**。
- 最后**必须**调一次 `publish_review`，即使没找到问题也要发个"clean PR"评论。

Reviewer 没有 commit / push 工具——它是只读的。

### 2.4 `agent/webapp.py` —— FastAPI Webhook 层

这是文件最大的一个（2463 行）但结构很规整。读它建议按"入口路由 → 解析 → 派发"三层看：

| 路由 | 处理函数 | 干啥 |
|---|---|---|
| `POST /webhooks/linear` | `linear_webhook` | 校验签名 → 判 `Comment.create` → 查是否 `@openswe` → 拉 issue 详情 → `process_linear_issue` |
| `POST /webhooks/slack`  | `slack_webhook`  | Slack URL 验签 + Event API → `process_slack_mention` 或 `process_slack_pr_review_request` |
| `POST /webhooks/github` | `github_webhook` | 多 event 大分发：issue、issue_comment、pull_request_review_comment、push 等 |
| `GET /health` | `health_check` | 给负载均衡用 |

核心通用流程：

1. **校验签名**（GitHub 用 HMAC-SHA256，Slack 用 timestamp+sig，Linear 用 secret）；
2. **生成确定性 `thread_id`** —— 这是巧思！同一 Linear issue / 同一 Slack thread / 同一 PR branch 永远映射到同一个 `thread_id`，于是后续消息会路由进**同一个运行中的 agent**，复用同一个沙箱（见 `generate_thread_id_from_issue`、`generate_thread_id_from_slack_thread`、`generate_reviewer_thread_id`）。
3. **仓库白名单 + 公共仓组织门禁**：`_is_repo_allowed`、`_enforce_public_repo_org_gate` 防止陌生人把你的 agent 调起来。
4. **触发或排队**：`_trigger_or_queue_run`
   - 如果 thread 当前没在跑（`is_thread_active`==False），直接 `client.runs.create(...)` 起一次新运行。
   - 如果正在跑，就把消息塞进 LangGraph store 里的"消息队列"（命名空间 `("queue", thread_id)`），由 `check_message_queue_before_model` 中间件在下一次模型调用前注入。

> **设计精髓**：webhook 永远不阻塞、永远幂等。"如果 agent 在忙，就排队等它消费"——这是为什么用户能边等边追加补充信息。

### 2.5 `agent/prompt.py` —— System Prompt 构造器

不是单纯模板字符串拼接，它会做几件具体的事：

- 注入工作目录 `{working_dir}`、Linear/Slack 触发者身份；
- 读取**外挂的 `default_prompt.md`**（项目根目录），允许用户覆盖默认提示而不动代码；
- 拼装"危险标签"`<dangerous-external-untrusted-users-comment>`——把网友写的 GitHub issue 内容包起来，防止 prompt injection。

### 2.6 `agent/encryption.py` —— Token 加密

`MultiFernet([Fernet(k) for k in keys])` 支持**多 key 滚动解密**：你可以加新 key 在前面，旧 key 留在后面用来解历史数据。Token 加密结果直接存进 thread metadata，所以即使 LangGraph dev 重启，用户 OAuth token 不会丢。

### 2.7 `agent/reviewer_*.py` 三件套

| 文件 | 责任 |
|---|---|
| `reviewer_findings.py` | Finding 的 TypedDict、严重度排序、读写 thread metadata 的 CRUD。**所有 findings 都存在 metadata 上**，不依赖外部 DB。 |
| `reviewer_diff.py` | 把 unified diff 解析成 `{file: set(line_numbers)}`，让 `add_finding` 在 LLM 给出"不在 diff 里"的行号时立刻报错（早期失败比晚到 GitHub 才 403 强）。 |
| `reviewer_publish.py` | 调 GitHub REST `POST /pulls/{n}/reviews` 一次性提交所有 inline 评论；调 GraphQL `resolveReviewThread` 关闭已解决的旧线程。 |

---

## 3. `agent/tools/` —— 给 LLM 的工具箱

> Deep Agents 框架已经内置了 `read_file / write_file / edit_file / ls / glob / grep / execute / task`（子代理）。这里只放**业务专属工具**，刻意精简。

### 3.1 工具一览（按用途分组）

**网络访问**
- `http_request` — 通用 HTTP 客户端。**重点是它的 SSRF 防护**：把 `socket.create_connection` monkey-patch 成 DNS-pin 版本，避免恶意 DNS 在重定向中切到内网 IP。
- `fetch_url` — `http_request` 的 GET 包装 + `markdownify` 把 HTML 转成 Markdown，省 token。
- `web_search` — 调 Exa API 搜网页。

**沟通回执**
- `slack_thread_reply` — 给当前 Slack thread 回消息；会把 `@User` 转成 `<@UID>` 格式；会把 message_ts 存进 LangGraph store 做"哪条 Slack 消息对应哪次 run"映射。
- `slack_read_thread_messages` — 拉历史。
- `linear_comment` / `linear_create_issue` / `linear_get_issue` / `linear_get_issue_comments` / `linear_list_teams` / `linear_update_issue` / `linear_delete_issue` — Linear GraphQL 的 7 个常用动作。

**PR 流程**
- `request_pr_review` — 让另一条 reviewer 线程开干（通过 `client.runs.create` 触发 reviewer 图）。

**Reviewer 专用**（不在主 agent 里）
- `add_finding` — 写一条 findings；会校验严重度枚举 + （首评时）行号必须落在 diff 内。
- `update_finding` — 改既有 finding 的 severity/description/status。
- `list_findings` — 给 LLM 看自己已经记了哪些。
- `publish_review` — 终点站：把所有 findings 一次性发到 GitHub，并把每条 finding 的 `github_review_comment_id` 回写到 metadata，方便下次再评时关线程。

### 3.2 工具长什么样

工具就是带 docstring 的普通 Python 函数。LLM 看到的是 *函数签名 + docstring*，所以**docstring 是 prompt 的一部分**——任何指引都直接写在那里：

```python
def linear_comment(comment_body: str, ticket_id: str) -> dict[str, Any]:
    """Post a comment to a Linear issue.

    Use this tool to communicate progress and completion to stakeholders on Linear.

    **When to use:**
    - After opening/updating a draft PR, post a comment on the Linear ticket to let
      stakeholders know the task is complete and include the PR link. For example:
      "I've completed the implementation and opened a PR: <pr_url>"
    - When answering a question or sharing an update (no code changes needed).
    """
    success = asyncio.run(comment_on_linear_issue(ticket_id, comment_body))
    return {"success": success}
```

> **加新工具的 checklist**：
> 1) 在 `agent/tools/` 加文件，函数 + docstring（务必写"When to use"）；
> 2) 在 `agent/tools/__init__.py` 里 import + 加进 `__all__`；
> 3) 在 `agent/server.py:get_agent` 的 `tools=[...]` 列表里加上；
> 4) 在 `agent/middleware/refresh_slack_status.py:_TOOL_STATUS` 字典里给个"AI is doing X..."文案；
> 5) 写 pytest（参考 `tests/test_*_tool*.py`）。

---

## 4. `agent/middleware/` —— 模型/工具调用的钩子

中间件是 LangChain 的 `AgentMiddleware` 或装饰器 `@before_model / @after_model / @after_agent`。它们在 *agent loop 的特定时机* 被调用，可以读写 state、改请求、加消息。

| 文件 | 钩子点 | 解决什么问题 |
|---|---|---|
| `sanitize_tool_inputs.py` | `wrap_tool_call` | LLM 写出 `offset='1, 80'` 这种胡乱 int → 提取首段数字传给工具，避免白白浪费一轮 |
| `tool_error_handler.py` | `wrap_tool_call` | 工具抛异常 → 转成 `ToolMessage(status="error", ...)` 让 LLM 自己看错读再重试；**特别处理 `SandboxClientError`：触发上层重建沙箱** |
| `check_message_queue.py` | `@before_model` | 从 `("queue", thread_id)` namespace 拉出排队中的用户新消息，注入成新的 HumanMessage（多模态 image_urls 也会被拉成 image block） |
| `refresh_slack_status.py` | `wrap_model_call`+heartbeat | 每 60s 给 Slack 发一次 "assistant.threads.setStatus"，让"AI 正在工作"提示框不消失 |
| `ensure_no_empty_msg.py` | `@after_model` | LLM 不调工具就空话回复 → 强行补一个 `no_op` ToolCall，避免循环提前死掉；或加 `confirming_completion` ToolCall 让模型再确认一次 |
| `notify_step_limit.py` | `@after_agent` | 触顶时主动发 Slack，告诉用户"我撞墙了" |
| `sandbox_circuit_breaker.py` | 自定义循环 | 同一沙箱连续 2 次出 `SandboxClientError` → 不再重建，直接给用户回"请重新触发" |
| `model_fallback.py` | `wrap_model_call` | 主模型出 5xx/429/超时 → 用绑了同样工具的备用模型再试一次 |
| `exclude_tools.py` | `wrap_model_call` | 在 reviewer 里禁用 `task`（子代理）工具——审查不需要 fanout |

> **顺序的副作用**：在 `server.py` 里中间件列表的顺序就是它们"包裹"agent loop 的顺序。最外层（写在最前）的最先接到调用、最后看到结果。

---

## 5. `agent/utils/` —— 集成层的脏活累活

这一层不写业务逻辑，只把 GitHub / Slack / Linear / Sandbox / Token 这些"外面世界"的 API 封装好。我按"主题"切片：

### 5.1 GitHub 系列

| 文件 | 干啥 |
|---|---|
| `github_app.py` | 用 `GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY` 签 JWT，换 installation access token（1h 有效） |
| `github_token.py` | thread metadata 里的加密 OAuth token 的读/解密/过期判断（带 60s skew） |
| `github_comments.py` | 解析 `@open-swe` 提及、`review <url>` 命令；过滤 `<dangerous-external-untrusted-users-comment>` 标签防 prompt 注入；HMAC 校验 webhook |
| `github_org_membership.py` | 公共仓组织门禁：`https://api.github.com/orgs/{org}/members/{user}` |
| `github_user_email_map.py` | 在 LangSmith Cloud bot-token-only 模式下，把 GitHub login 映射到 LS email（小型字典 + 兜底逻辑） |

### 5.2 Sandbox 系列

| 文件 | 干啥 |
|---|---|
| `sandbox.py` | 总入口：根据 `SANDBOX_TYPE` 环境变量分发到具体 integration |
| `sandbox_state.py` | `SANDBOX_BACKENDS` 全局 dict + `SandboxBackendProxy`（**这是一个"代理对象"**，agent 始终持有 proxy.id 稳定不变，背后 `_backend` 可以热替换；这就让中间件能在不重启 graph 的前提下重连沙箱） |
| `sandbox_paths.py` | 解析沙箱内的工作目录（`/workspace` 或 `/repo` 之类） |

### 5.3 Auth 系列

| 文件 | 干啥 |
|---|---|
| `auth.py` | **核心**：`resolve_github_token(config, thread_id)`。先解 metadata 加密 token；如果过期 → 走 LangSmith 短期 JWT 拉 GitHub OAuth；如果没配 OAuth → 退到 GitHub App installation token（bot-token-only 模式）；都失败就给用户私聊一条"请去鉴权"的链接 |
| `authorship.py` | 把 Slack / Linear 触发者解析成 `CollaboratorIdentity`，给 prompt 注入"你是替 Alice 干活" |

### 5.4 Slack / Linear

| 文件 | 干啥 |
|---|---|
| `slack.py` | 850 行的大型工具库：发消息、读 thread、维持 assistant status、把 `@Name` 转换成 `<@UID>`、把 image URLs 转 multimodal block… |
| `slack_feedback.py` | "👍 / 👎 反馈"埋点 |
| `linear.py` | Linear GraphQL 包装：留言、查 issue、改状态 |
| `linear_team_repo_map.py` | `LINEAR_TEAM_REPO_MAP` 环境变量解析：哪个 Linear team 默认对哪个 GitHub repo |

### 5.5 其他

- `model.py` —— `make_model()` 工厂：根据 `openai:gpt-5.5` 这类前缀分流到不同 ChatModel；处理 `reasoning={"effort":..."}` 等 provider 特有参数。
- `messages.py` —— 把 LangChain message 的多模态 content block 拍平成纯文本，给日志/Slack 用。
- `multimodal.py` —— 把 Slack/Linear 附件的 image URL 转成 LangChain `image` block。
- `repo.py` —— 解析 `owner/repo`、规范化 PR 链接、判断 fork。
- `langsmith.py` —— LangSmith member / workspace 接口包装。
- `comments.py` —— 通用 markdown 段落清洗。

---

## 6. `agent/integrations/` —— 沙箱后端工厂

每个文件都很短，只暴露一个 `create_xxx_sandbox(sandbox_id=None)`：

| 提供商 | 文件 | 说明 |
|---|---|---|
| **LangSmith**（默认） | `langsmith.py` | 调 `SandboxClient`，能配 *proxy_config* 把 `github.com` 流量自动加 Authorization；可加载快照大幅提速冷启动 |
| Modal | `modal.py` | `modal.Sandbox.create(app)` |
| Daytona | `daytona.py` | 用 `langchain_daytona` |
| Runloop | `runloop.py` | 用 `langchain_runloop` |
| Local | `local.py` | **⚠️ 没有隔离**，直接在你机器上执行 shell——仅本地开发用 |

切换只需 `SANDBOX_TYPE=modal` 一行环境变量，**业务代码零改动**。

> **加新后端的 checklist**：
> 1) 写一个 `agent/integrations/foo.py` 暴露 `create_foo_sandbox()`，返回任何实现 `SandboxBackendProtocol` 的对象（这是 deepagents 定义的协议）；
> 2) 在 `agent/utils/sandbox.py:SANDBOX_FACTORIES` 字典里加一行；
> 3) 如果有专属 env 校验，在 `validate_sandbox_startup_config()` 里加分支。

---

## 7. `tests/` —— pytest 单元测试

测试都是 unit-only，**`asyncio_mode = "auto"`** 让你能直接 `async def test_xxx()`。

按"测试谁"切片：

- `test_auth_sources.py / test_github_token_ttl.py / test_proxy_auth.py / test_public_repo_org_gate.py` —— 鉴权链路
- `test_encryption.py` —— Fernet 多 key 滚动
- `test_http_security.py` —— SSRF 防护
- `test_repo_extraction.py / test_recent_comments.py / test_github_comment_prompts.py / test_github_issue_webhook.py` —— webhook 解析
- `test_langsmith_sandbox_config.py / test_sandbox_paths.py` —— 沙箱集成
- `test_slack_*.py` —— Slack 各路径
- `test_reviewer*.py` —— Reviewer 图、findings、diff 校验、publish、watch 模式
- `test_multimodal.py` —— 多模态内容拼装
- `middleware/test_sandbox_recovery.py` —— 圈断电路重连场景

跑测试：
```bash
make test                                            # 全部
make test TEST_FILE=tests/test_encryption.py         # 单文件
uv run pytest -vvv tests/test_reviewer.py::test_X    # 单测
```

---

## 8. `evals/reviewer/` —— Reviewer 的 LLM-as-judge 评测

这是项目里"质量保证"的另一极：**让 LLM 评 LLM**。

```
evals/reviewer/
├── golden_comments/        # 5 个真实开源仓的"金标评论"（人类写过的好 PR review）
│   ├── cal_dot_com.json
│   ├── discourse.json
│   ├── grafana.json
│   ├── keycloak.json
│   └── sentry.json
├── build_dataset.py        # 把 golden_comments 拼成 LangSmith 数据集
├── target.py               # 跑一次 reviewer 图，输出它给出的 findings
├── judge.py                # 让 judge model 比较 model-output vs. golden，给分
├── run_eval.py             # 把上面三步串起来：build → run target → judge
└── README.md
```

用法（README 里有详细说明）：先 `build_dataset.py` 上传，再 `run_eval.py` 触发。每次改 reviewer prompt 或 LLM 都可以跑一次回归。

---

## 9. `scripts/` —— 一次性运维脚本

- `create_sandbox_snapshot.py` —— 起一个临时沙箱，预装常用工具（gh、uv、node…），拍快照，把 `DEFAULT_SANDBOX_SNAPSHOT_ID` 写回 `.env`。**这是冷启动从 60s 降到 5s 的核心招**。
- `list_snapshots.py` —— 列出当前 LangSmith workspace 的全部快照。
- `check_pr_merge_status.py` —— 给一个 PR 列表，并发去查它们 merge 状态，用于看 Open SWE 自己提的 PR 通过率。

---

## 10. 一次完整请求的调用链（合在一起看）

> 场景：用户在 Slack 里 `@open-swe 帮我修一下 server.py 的内存泄漏`。

```
1. Slack 发 event API → POST /webhooks/slack (agent/webapp.py)
2. webapp.slack_webhook
   ├─ verify_slack_signature
   ├─ 解析事件 → channel_id, thread_ts, message
   ├─ 调 get_slack_repo_config 决定是哪个 repo
   ├─ generate_thread_id_from_slack_thread(channel_id, thread_ts)   # 确定性 ID
   └─ background_tasks.add_task(process_slack_mention, ...)
3. process_slack_mention
   ├─ 拉 Slack thread 历史
   ├─ 拼装 user_prompt
   └─ _trigger_or_queue_run
        ├─ 如果 thread 没活跃 → client.runs.create(graph="agent", config={...})
        └─ 如果在跑 → store.put(("queue", thread_id), {"messages":[...]})
4. LangGraph 收到 runs.create → 调 agent.server:get_agent(config)
   ├─ resolve_github_token(...)               # 拿 OAuth token / app token
   ├─ ensure_sandbox_for_thread(thread_id)    # 四态机
   │   └─ 必要时 _create_sandbox_with_proxy() + _configure_github_proxy()
   ├─ construct_system_prompt(...)
   └─ return create_deep_agent(model, prompt, tools, backend, middleware)
5. Deep Agent loop 跑起来
   ├─ before_model: check_message_queue_before_model 把队列消息注入
   ├─ LLM 调用 → fallback 中间件 / 错误就切备用模型
   ├─ LLM 生成 tool_call: execute("gh repo clone ...")
   │   ├─ wrap_tool_call: SanitizeToolInputsMiddleware (跳过 execute)
   │   ├─ wrap_tool_call: SlackAssistantStatusMiddleware ("running commands...")
   │   ├─ wrap_tool_call: ToolErrorMiddleware (try/except)
   │   └─ backend.execute(...)  ── 在沙箱里跑，GH_TOKEN=dummy 被代理改写成真 token
   ├─ … LLM 多轮 read_file / edit_file / execute ...
   ├─ tool_call: slack_thread_reply("已完成，PR 在: https://...")
   └─ tool_call 没了 → 触发 after_model:
       ├─ ensure_no_empty_msg: 看是不是空话回复
       └─ notify_step_limit_reached: 看是不是触顶
6. agent 退出 → run 完成 → Slack 用户看到一条最终消息 + PR 链接
```

> 如果用户在第 5 步进行中**又发了一条 Slack 消息**：
> - webapp 会发现 thread 还活跃，把消息丢进 store 的 queue；
> - 下一次 `before_model` 时，`check_message_queue_before_model` 把它注入成新的 HumanMessage；
> - LLM 看到新需求，自然地接着办。

---

## 11. 常见二次开发任务速查

| 需求 | 改动点 |
|---|---|
| 加新工具（例如调内部 ticket 系统） | 写 `tools/foo.py` → 在 `tools/__init__.py` 导出 → `server.py` 的 `tools=[...]` 加进去 → `refresh_slack_status.py` 加状态文案 → 写测试 |
| 加新沙箱后端 | `integrations/foo.py` 实现 `create_foo_sandbox` → `utils/sandbox.py` 的 `SANDBOX_FACTORIES` 加一行 → 加启动校验 |
| 加新中间件 | `middleware/foo.py` → `middleware/__init__.py` 导出 → `server.py` / `reviewer.py` 的 `middleware=[...]` 按顺序插入 → 写测试 |
| 改 Prompt | 编辑 `agent/prompt.py` 的 `WORKING_ENV_SECTION` 等常量，**或**在项目根放 `default_prompt.md` 走"用户覆盖"通道 |
| 加新触发入口（比如 Telegram） | 在 `webapp.py` 加 `/webhooks/telegram` 路由 + 签名校验 + `generate_thread_id_*` + `_trigger_or_queue_run` |
| 改严重度的发布门槛 | `publish_review(severity_threshold="high")` 默认是 medium，可以做成 thread metadata 配置 |
| 把 GitHub App 切换成 OAuth 模式 | 设置 `X_SERVICE_AUTH_JWT_SECRET` + `USER_ID_API_KEY_MAP` 后，`is_bot_token_only_mode()` 会自动返回 False |

---

## 12. 几条容易踩的坑

1. **`langgraph dev` vs `make run`**：`make run` 只起 FastAPI（webhook 服务），**不带图**；想完整跑起来必须 `make dev`（langgraph dev）。线上是 `langgraph cloud` 同时托管两者。
2. **Token 加密 key 丢了等于线上线程全废**：`TOKEN_ENCRYPTION_KEY` 要持久化，并且滚动新增 key 时**新 key 放最前**，旧 key 不要删（`MultiFernet` 才能解历史数据）。
3. **沙箱 ID 不能和 `__creating__` 混淆**：`get_sandbox_id_from_metadata` 返回 `"__creating__"` 是哨兵值，不是真 ID。
4. **添加新工具但忘了改 `_TOOL_STATUS`**：Slack 进度条会卡在上一个工具的文案，不影响功能但用户体验差。
5. **Reviewer 的 `add_finding(start_line=...)` 一定要落在 diff 内**：否则 GitHub API 会 422。**但首评时**已经把 `diff_line_set` 设成 None（见 `reviewer.py:306`），跳过该校验，让 LLM 自取 diff——这是历史 hotfix，要明白当前实际行为再改。
6. **prompt 注入风险**：所有从 GitHub issue / Slack message 来的文本必须先用 `<dangerous-external-untrusted-users-comment>` 包起来。新增触发渠道时别忘了走同一套清洗。

---

## 13. 推荐的阅读顺序

1. `README.md`（架构概览）→ `CLAUDE.md`（agent 文化）→ `CUSTOMIZATION.md`（要换哪些零件）。
2. `agent/server.py`（10 分钟读完，再回头看每个引用）。
3. `agent/webapp.py` 的三个路由函数 + 它们各自的 `process_*`。
4. 任选一个工具（推荐 `slack_thread_reply.py` 或 `publish_review.py`）跟到底。
5. 任选一个中间件（推荐 `check_message_queue.py`）理解 LangGraph 钩子怎么用。
6. `agent/utils/sandbox_state.py` + `integrations/langsmith.py` 理解代理热替换。
7. `tests/` 里挑两个测试随便看，知道这套代码该怎么验证。

---

## 14. 一图回顾全局

```
                                    LangGraph dev server (单进程)
   ┌──────────────────────────────────────────────────────────────────────────┐
   │                                                                          │
   │   FastAPI                          LangGraph 图运行时                       │
   │   ┌─────────────────┐              ┌────────────────────────────────┐    │
   │   │ /webhooks/slack │ ─┐           │  get_agent(config) ───────────┐│    │
   │   │ /webhooks/linear│ ─┤  triggers │  ensure_sandbox_for_thread    ││    │
   │   │ /webhooks/github│ ─┴─────────► │  make_model + prompt + tools  ││    │
   │   └─────────────────┘              │  middleware stack             ││    │
   │                                    │  → create_deep_agent (Pregel) ││    │
   │   存储 (LangGraph store)            └────────────────────────────────┘    │
   │   ─ thread metadata: sandbox_id, github_token_encrypted, findings...     │
   │   ─ queue namespace : 运行中投递的用户新消息                                │
   │                                                                          │
   └──────────────────────┬─────────────────────────────────────────┬─────────┘
                          │                                         │
                          ▼                                         ▼
                外部服务 (Slack/Linear/GitHub/Exa)            云沙箱 (LangSmith/Modal/Daytona/Runloop)
                                                              ├ shell + gh CLI
                                                              ├ HTTP 代理注入 GitHub 凭证
                                                              └ 工作目录里是 clone 好的 repo
```

至此你已经掌握 Open SWE 的全部"零件名 + 装配关系"，可以放心打开任意文件开始改造了。
