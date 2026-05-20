# OpenBot · Slice E — Input-Side Completeness

> Status: **Archived — 已完成（2026-05-20）**
> 实现 commit: `5b34158 feat(middleware): input-side G1-G4 (sanitize/audit/feature_toggle, dispatch chain order)`
> 验证: `build_preflight_chain()` 输出与 spec §3 M3 + §5 amendments 一致：
> `sanitize_inputs → kill_switch → feature_toggle → cancel_label → cancel_comment → fork_pr_gate → actor_role → rate_limit → budget → audit_start`
> 起草日期：2026-05-17
> 上游：[`../../prd/openbot-harness-spec.md`](../../prd/openbot-harness-spec.md)（slice A–D 已 land）
> 配套：[`../../prd/openbot-prd.md`](../../prd/openbot-prd.md) §4 / §5 / §8.4 / §13
> 目标（原）：**关闭 spec slice A–D 之后剩下的 input-side gap**，让"agent 输入之前"完全收口；不引入新业务面。

---

## 0. Baseline · slice A–D 实际状态（与 spec 对照）

| Spec 锚点 | 模块 | 状态 |
|---|---|---|
| §3 M1 | `openbot.infrastructure.config_loader.py` — yaml.safe_load + 60s TTL cache | ✅ |
| §3 M2 | `openbot.application.router.py` — `dispatch_for` + `derive_task_id` | ✅ |
| §3 M3 | `openbot.application.middleware/preflight.py` — runner + `announce_once` | ✅ |
| §3 M4 | `openbot.application.middleware/cancel.py` — KillSwitch / CancelLabel / CancelComment | ✅ |
| §3 M5 | `openbot.application.middleware/budget.py` — monthly_soft + global_hard | ✅ |
| §3 M6 | `openbot.application.middleware/rate_limit.py` — per-user-day / per-repo-hour | ✅ |
| §3 M7 | `openbot.application.middleware/security.py` — ForkPRGate / ActorRole | ✅ |
| §3 M8 | `openbot.infrastructure.llm/sanitize.py` — `wrap_user_input(text, source)` | 🟡 函数有，调用方与 lint 缺 |
| §3 M9 | `openbot.application.workflows/_lifecycle.py` — STARTED/COMPLETED/FAILED | 🟡 STARTED 写在 handler 内（见 G2） |
| §3 M10 | `openbot.infrastructure.queue/{enqueue,worker,runner,payload}.py` | ✅ |
| §3 M11 | `openbot.application.workflows/chat_parser.py` | ✅ |
| §3 M12 | `openbot.application.workflows/{review,fix,chat,triage}.py` stubs | ✅ |

**当前 chain（`openbot.application.dispatcher.py:51-66`）**：
```
KillSwitch → CancelLabel → CancelComment → ForkPRGate → ActorRole → RateLimit → Budget → handler
```

**Spec 锁定 chain（§3 M3）**：
```
SanitizeInputs → KillSwitch → CancelLabel → ForkPRGate → ActorRole → RateLimit → BudgetCheck → AuditStart → handler → AuditEnd
```

两边的差异即本 plan 要消除的 G1-G6。

---

## 1. 目标与非目标

### 1.1 目标

1. **链顺序与 spec §3 M3 锁定完全一致**，外加 spec 未列入但实际有用的 `CancelCommentMiddleware`（写入 spec amendment）。
2. **`SanitizeInputs` middleware 化**（chain 第 1 格）+ **`AuditStart` middleware 化**（chain 第 8 格）+ **`FeatureToggleMiddleware`**（spec §3 M1 隐含承诺：`features.chat=false` 必须真的 BLOCK）。
3. **Prompt-injection 静态防线**：lint 测试杜绝 `event.comment_body / event.raw[...]` 直接拼进 LLM messages 的可能。
4. **Spec §7 八条 demo 全部以脚本形式固化为 E2E 测试**，每条对应一个失败必报警的 pytest case。
5. **上手自检**：`make doctor` 跑完三件事（ENV 完整性 / Postgres+Redis 可达 / 模拟 webhook 走完 chain）。
6. **`.env.example` 与 Settings 字段对齐**（`OPENBOT_WORKER_CONCURRENCY` 等漏项补齐）。

### 1.2 非目标

- 不接 LLM 真调用（agent slice 的事）
- 不引入 bandit / mypy / coverage gate（PRD §8.4 锁定排除）
- 不动 LangGraph / Modal / DeepAgent（仍是 Week 3+）
- 不做 `.openbot/config.yaml` 高风险字段 `config-approved` 闸（推 v0.2）
- 不动 Slack / Linear / Discord adapter（v0.2）

---

## 2. Gap 清单（G1–G6）

| # | Gap | 影响 | 新建路径 |
|---|---|---|---|
| G1 | `SanitizeInputs` 不是 middleware；用户输入未在 chain 入口做格式过滤 | 一旦 agent 接入易遭 zero-width / RTL / 超长字段绕过 | `openbot.application.middleware/sanitize.py` |
| G2 | `AuditStart` 不在 chain；handler 进入前 crash 没有 STARTED 行 | 审计缺一段，事后无法判定 "preflight 过了但 handler 没跑" | `openbot.application.middleware/audit.py` |
| G3 | `FeatureToggleMiddleware` 缺失；`features.chat=false` 不会 BLOCK | spec §3 M1 隐含承诺破裂；用户改 yaml 不生效 | `openbot.application.middleware/feature_toggle.py` |
| G4 | Prompt-injection lint 没建；workflow 改动后注入面无静态护栏 | spec §3 M8 Acceptance 第 3 条未满足 | `tests/test_no_raw_user_input.py` |
| G5 | Spec §7 八条 demo 未脚本化 | 回归网破洞——任何 chain 改动都靠手动复现 | `tests/e2e/test_spec_demos.py` |
| G6 | `make doctor` 缺失；`.env.example` 漏 `OPENBOT_WORKER_CONCURRENCY` 等 | 首跑用户排查成本高；env-覆盖路径不可发现 | `scripts/doctor.py` + `Makefile` + `.env.example` |

---

## 3. 详细规格

### G1. `SanitizeInputs` middleware — `openbot.application.middleware/sanitize.py`

**目的**：在 chain 第 1 格对 `UnifiedEvent` 携带的所有 user-controlled string（`title`、`body`、`comment_body`、`actor`、`labels`）做一次入口级别的过滤；这是 spec §3 M8 LLM-side `wrap_user_input` 的**上游**——前者管"危险字节进系统"，后者管"危险结构进 LLM"。

**接口**

```python
@dataclass(frozen=True, slots=True)
class SanitizeInputsMiddleware:
    name: str = "sanitize_inputs"
    max_field_length: int = 8_000      # 单字段截断阈
    max_total_length: int = 64_000     # 全字段拼接阈（防 DoS）

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision: ...
```

**过滤规则**（统一封装在 `_sanitize_string(text: str) -> str`）

| 项 | 处理 |
|---|---|
| Unicode `Cf`（format） / `Cc`（control，除 `\n\t`） | 删 |
| RTL override（U+202E）、U+200B-200F | 删 |
| 单字段超 `max_field_length` | 截断 + audit `details["truncated"]=field_name` |
| 全字段拼接超 `max_total_length` | BLOCKED `reason="input_too_large"`, audit phase = REJECTED |
| `actor` 未通过 `^[A-Za-z0-9-]{1,39}$` | BLOCKED `reason="actor_invalid"`（防 Redis key injection） |

**实现要点**

- 不写回 `event`（frozen dataclass）：把"已清洗"版本存入 `ctx.cache["sanitized_event"]`；下游 middlewares 与 workflow 通过 helper `sanitized_event(ctx) -> UnifiedEvent` 取用。
- 不引入 `bleach`：bleach 是 HTML sanitizer，过度匹配且重；本 case 是受控字段集，手写更明确。
- 测试：`tests/middleware/test_sanitize.py` ≥ 8 cases。

**Acceptance**

- [ ] `‎` / `‮` 出现在 `comment_body` 时被静默删除（不 BLOCK，仅过滤）
- [ ] `actor="../etc/passwd"` → BLOCKED `actor_invalid`
- [ ] 64KB payload → BLOCKED `input_too_large`，audit phase=REJECTED
- [ ] 8KB 单字段 → 截断 + `details.truncated="comment_body"`
- [ ] 清洗后的 event 可被后续 middleware 读到（`ctx.cache["sanitized_event"]`）

---

### G2. `AuditStart` middleware — `openbot.application.middleware/audit.py`

**目的**：在 chain 末尾、handler 之前**强制**写一行 `audit_log(phase=STARTED, …)`。任何 handler 入口前的异常都能在 audit 表里看到 STARTED-无配对，便于事后定位。

**接口**

```python
class AuditStartMiddleware:
    name = "audit_start"
    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision: ...
```

**实现要点**

- 复用 `openbot.application.workflows/_lifecycle.py:_write_phase`，写一行 `STARTED`。
- 失败不 BLOCK：DB 挂时返回 PROCEED + WARNING log（与现有 fall-open 策略一致）。
- 同时把 `audit_lifecycle` 内部的 STARTED 写入改成 **"如果 ctx.cache['audit_started']=True 就跳过"**，避免双写。
  - `AuditStartMiddleware` 写完后 `ctx.cache["audit_started"] = True`；
  - `audit_lifecycle` 进入时检查 `ctx.cache.get("audit_started")`，已写则只管 COMPLETED/FAILED。

**Acceptance**

- [ ] Workflow handler 抛 ImportError 时 audit 表仍有 STARTED 行（无 COMPLETED / FAILED 配对）
- [ ] `audit_lifecycle` 不重复写 STARTED
- [ ] DB unconfigured → middleware 返回 PROCEED，handler 仍执行

---

### G3. `FeatureToggleMiddleware` — `openbot.application.middleware/feature_toggle.py`

**目的**：把 `EffectiveConfig.features.{triage,review,fix,chat}` 真正变成开关。

**接口**

```python
class FeatureToggleMiddleware:
    name = "feature_toggle"
    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision: ...
```

**实现要点**

- 读 `ctx.dispatch.feature`（已经是 enum），查 `ctx.config.features` 对应字段。
- `False` → BLOCKED `reason=f"feature_disabled:{feature}"`，audit phase=SKIPPED，不回写评论（用户自己关的不必反馈）。
- 放在 `KillSwitch` 之后、`CancelLabel` 之前——比 cancel-label 便宜，比 kill-switch 贵。

**Acceptance**

- [ ] `features.chat=false` + `@openbot ping` → BLOCKED；audit `outcome="feature_disabled:chat"`
- [ ] 全 true（默认）→ PROCEED
- [ ] 单测 4×2 = 8 case（feature × on/off）

---

### G4. Prompt-injection lint — `tests/test_no_raw_user_input.py`

**目的**：spec §3 M8 Acceptance 第 3 条——杜绝 `event.comment_body` / `event.raw[...]` 在 `openbot.application.workflows/*.py` 与 `openbot.infrastructure.llm/*.py` 里**直接**作为 string 进入 LLM messages。

**实现**

- 用 `ast` 模块扫描 `openbot.application.workflows/` 和 `openbot.infrastructure.llm/` 全部 `.py`：
  - 找 `Attribute` 节点 value=`Name("event")` attr 在 `{"comment_body", "title", "body"}` 中
  - 找 `Subscript` 节点 value 形如 `event.raw[...]`
  - 检查这些表达式是否被传入 `wrap_user_input(...)` 调用、字面常量赋值（如 `_ACK_TEMPLATE = "..."`）或日志 `extra={...}` 字典 —— 这些是允许场景
  - 其余视为违规，pytest 失败并列出 `file:line`
- 允许 list 维护在 `tests/_lint_allowlist.py`，每条带 ADR 链接（v0.1 留空）。

**Acceptance**

- [ ] 在 `chat.py` 临时写一行 `f"{event.comment_body}"` 进 message → 测试失败
- [ ] 现有 `_ACK_TEMPLATE.format(actor=event.actor or "there")` → 不违规（actor 已过 G1 校验）
- [ ] 测试本身 < 200 行

---

### G5. Spec §7 八条 demo 固化 — `tests/e2e/test_spec_demos.py`

**目的**：spec §7 的八条 demo 必须有线性 pytest 脚本，回归才能自动化。

**结构**

```python
# tests/e2e/conftest.py
@pytest.fixture
async def webhook_harness(...) -> WebhookHarness:
    """Composes: TestClient(app) + fakeredis + sqlite-memory + stub GitHubAdapter.
    Stub adapter records every reply()/label()/role-query so tests assert side-effects.
    """
    ...
```

| Demo (spec §7) | Test ID | 验收 |
|---|---|---|
| 1 | `test_demo_01_issue_opens_triage_acks` | audit 表两行 STARTED+COMPLETED；adapter.reply 调用 1 次 |
| 2 | `test_demo_02_pr_opens_review_stub_acks` | 同上，feature=review |
| 3 | `test_demo_03_bot_assigned_fix_stub` | feature=fix |
| 4 | `test_demo_04_at_openbot_chat_ack` | feature=chat；非 cancel/help freeform 走 _ACK_TEMPLATE |
| 5 | `test_demo_05_cancel_label_blocks` | audit phase=SKIPPED, outcome=`cancel_label` |
| 6 | `test_demo_06_kill_switch_env_blocks` | `OPENBOT_KILL_SWITCH=true` monkeypatched；audit REJECTED |
| 7 | `test_demo_07_fork_pr_default_off_ok_to_test_opens` | 两步：先 BLOCKED + announce；evolution 后 PROCEED |
| 8 | `test_demo_08_rate_limited_user_sees_single_comment` | 21 次请求；adapter.reply 命中 announce_key 仅 1 次 |
| 9 | `test_demo_09_worker_restart_does_not_drop_message` | XADD → kill consumer task → restart → 仍消费一次（用 pytest-asyncio + fakeredis Stream） |

**Acceptance**

- [ ] 9 个测试全部独立可跑（不依赖外部 GitHub / Redis / Postgres）
- [ ] `make check` 包含它们（属 `tests/`，pytest 默认收集）
- [ ] 每条测试 ≤ 60 行；共用 fixture 控制重复

---

### G6. `make doctor` + `.env.example` 收尾 — `scripts/doctor.py`

**目的**：用户跑完 `make setup` 后能用一条命令验证"全栈已就绪"。

**`scripts/doctor.py` 检查项**（顺序执行，遇错立即非 0 退出）

1. **ENV 完整性**：`OPENBOT_GITHUB_WEBHOOK_SECRET` / `OPENBOT_GITHUB_APP_ID` / `OPENBOT_GITHUB_APP_PRIVATE_KEY_PATH` 存在；PEM 文件可读且 `chmod 0600`。
2. **Postgres 可达**：用 `Settings.postgres_url` 建临时连接，`SELECT 1`。
3. **Redis 可达**：`PING`。
4. **Schema 已建**：`SELECT 1 FROM audit_log LIMIT 0` / `cost_meter` 都返回 0 行。
5. **签名往返**：用 webhook_secret 对一条假 issue payload 算 HMAC，POST 到 `/webhook/github`，期望返回 `status=accepted` 或 `ignored`。
6. **Worker 心跳**（可选，仅当 `--with-worker` 传入）：检查 Redis Stream `openbot.application.workflows` 的 `XLEN` 没有积压（< 100 entries 未 ACK）。

**`Makefile` 加目标**

```make
doctor: ## Run setup self-check (ENV, PG, Redis, schema, webhook round-trip)
	uv run python scripts/doctor.py
```

**`.env.example` 补齐**

```
# OPENBOT_WORKER_CONCURRENCY=4   # Redis-stream worker asyncio fan-out (spec §9.3)
# OPENBOT_MONTHLY_SOFT_CAP_USD=100  # override config.yaml budget.monthly_soft_cap_usd
```

**Acceptance**

- [ ] `make doctor` 在干净 docker-compose 起来后 5 秒内返回 ✓
- [ ] 故意 down 掉 Redis → `make doctor` 在第 3 步报错并非 0 退出
- [ ] `.env.example` `grep -c OPENBOT_` 与 `Settings` 字段数对齐（脚本兜底校验）

---

## 4. 切片计划

每个切片可独立 land 且端到端 demo-able。

| Slice | 范围 | 依赖 | 验收 demo |
|---|---|---|---|
| **E1** chain 完整化 | G1 + G2 + G3 + `dispatch.py` 重排 + 各 middleware unit test | A–D | `python -c "from openbot.application.dispatcher import build_preflight_chain; print([m.name for m in build_preflight_chain()])"` 输出 spec §3 M3 同序 8 格 |
| **E2** 验收脚本固化 | G5 八条 demo | E1 | `pytest tests/e2e/test_spec_demos.py -v` 9/9 ✓ |
| **E3** 注入面静态护栏 | G4 lint + `wrap_user_input` 在 chat workflow 内 dry-run（仅生成消息字符串，不调 LLM） | E1 | 在 `chat.py` 注入 `event.comment_body` 直拼，`make check` 红 |
| **E4** 上手体验 | G6 doctor + `.env.example` + Makefile | A–D（独立） | `make doctor` ✓；故意改坏 .env 后 ✗ |

**预估**：E1 ≈ 1d，E2 ≈ 1d，E3 ≈ 0.5d，E4 ≈ 0.5d = **3 天**

---

## 5. Spec amendments（本 plan land 后回填进 `openbot-harness-spec.md`）

| 锚点 | 改动 |
|---|---|
| §3 M3 链表 | 在第 3 格加 `CancelComment`（与 `CancelLabel` 并列；spec 原本只在 §3 M4 描述了 cancel-comment 路径） |
| §3 M3 链表 | 在第 2 格加 `FeatureToggle`（KillSwitch 之后、CancelLabel 之前） |
| §3 M3 链表 | 第 1 格 `SanitizeInputs` 与 §3 M8 的关系：前者是入口字节过滤；后者是 LLM-side 包裹 |
| §3 M9 | 显式说明 `AuditStart` 由 middleware 写、`audit_lifecycle` 只在未写过时补写 |
| §6 测试矩阵 | 加 `tests/middleware/test_sanitize.py`(8) / `test_feature_toggle.py`(8) / `test_audit_start.py`(3) / `tests/e2e/test_spec_demos.py`(9) / `tests/test_no_raw_user_input.py`(4)；预计总 cases +32 |

---

## 6. 横切关注点

### 6.1 安全

| 关注 | 实现 |
|---|---|
| Unicode `Cf` / `Cc` 过滤（G1） | 手写 `unicodedata.category` 白名单 — 不引 `bleach`（HTML sanitizer，scope mismatch） |
| 截断 + DoS 防护（G1 `max_total_length`） | 拼接 `sum(len(...))` 早断；不进 LLM 路径 |
| Redis key 注入（G1 actor 校验） | 与 §3 M6 同套白名单（`^[A-Za-z0-9-]{1,39}$`） |
| Doctor 不泄密 | 从不打印 PEM / webhook_secret 内容；仅 `presence: ok` |

> Security-Guidance 提到的 Helmet / DOMPurify / Tink / defusedxml 均与 OpenBot v0.1 attack surface 不匹配；YAML 已 `safe_load`，HTML 不渲染，crypto 用 stdlib `hmac.compare_digest`。**继续不引入**——按 PRD §8.4 锁定。

### 6.2 可观测性

- 所有新 middleware 写 audit 行的字段 schema 与现有保持一致（`phase / workflow / outcome / details`）
- doctor 输出走 stdout + 退出码；不写 audit_log

### 6.3 性能

- Sanitize middleware O(N) 单遍 + 截断；常数 ≪ DB / Redis 调用
- AuditStart 多写一行 DB → 与 `audit_lifecycle` 内现有 STARTED 是 1:1 替换，**净增 0**
- FeatureToggle 纯内存读，常数

---

## 7. 验收清单（端到端）

完成 E1–E4 后以下必须全部通过：

- [ ] `make check` 全绿（含新增 ~32 测试 case）
- [ ] `pytest tests/e2e/test_spec_demos.py -v` 9/9 ✓
- [ ] `python -c "from openbot.application.dispatcher import build_preflight_chain; print(','.join(m.name for m in build_preflight_chain()))"` 输出顺序与 spec §3 M3 + 本 plan §5 amendments 一致
- [ ] `make doctor` 在 `docker compose up -d && make dev` 起来后 5 秒内 ✓
- [ ] 故意在 `workflows/chat.py` 写 `messages.append(event.comment_body)` → `make check` 红
- [ ] `harness-spec.md` 的 §3 / §6 已按 §5 amendments 回填

---

## 8. 不在本 plan 的工作（明确推后）

- LangGraph + DeepAgent 接入 LLM workflow（Week 3+）
- Modal sandbox `SandboxBackend` ABC + 实现
- LangSmith workflow-level metadata
- `.openbot/config.yaml` 校验 CLI（`python -m openbot.infrastructure.config_loader validate <path>`）
- `config-approved` label 高风险字段闸（v0.2）
- pgvector / Issue dedup（v0.2）
- LinearAdapter / SlackAdapter / DiscordAdapter（v0.2）
- docker-compose 加 webapp/worker service（v0.1 release 镜像时再做）

---

## 9. 引用

- 上游 spec：[`../prd/openbot-harness-spec.md`](../prd/openbot-harness-spec.md) §3 M3 / §3 M8 / §7 / §9
- 上游 PRD：[`../prd/openbot-prd.md`](../prd/openbot-prd.md) §4.5 / §4.6 / §4.7 / §4.8 / §5.1 / §8.4
- Slice A–D 实现：`openbot/{config_repo,router,middleware,queue,workflows}/`
