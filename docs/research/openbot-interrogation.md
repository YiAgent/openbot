# OpenBot PRD 拷问清单

> 用途：在写 PRD 之前，把所有需要拍板的设计决策系统化列出。每一题后面都标了它的"级联影响"——为什么这题答错后面会一连串崩。
> 答题方式：先看 Theme 1-4 的"根基题"，这几题决定方向；再按主题展开。

---

## Theme 1 · 定位与商业（最先必答）

**1. 这个项目的根本定位是什么？**
- 选项 A：纯个人项目 / 简历作品 / 学 LangGraph
- 选项 B：开源项目，希望聚集 community，最终可能商业化
- 选项 C：商业 SaaS MVP，目标是融资或盈利
- 选项 D：内部工具，只服务你自己 / 公司的 repo

> 级联：影响 license、auth 复杂度、运维要求、是否要做计费、是否多租户。

**2. 你的目标用户是谁？（哪类人会装这个 bot）**
- 个人 OSS maintainer（一两个 repo）
- 小团队（3-10 人，10-50 repo）
- 企业（org-wide install，100+ repo）

> 级联：决定权限模型、配置 UX（YAML vs Web 表单）、是否做 SSO / RBAC。

**3. 主要差异化在哪？**（vs Copilot Coding Agent、OpenHands、Cursor BugBot、Devin）
- 开源 / 自托管
- 多 channel 一体化（Slack/Linear/GitHub 同后端）
- 可插拔（用户能加自己的 plugin）
- 价格 / 自带 API key
- 专注某个垂直（如只做 Python OSS / 只做 OSS maintainer）
- 没想清楚

> 级联：直接决定 README 的卖点、roadmap 优先级、要不要追 SOTA 分数。

**4. 商业化路径？**（如果不是纯个人）
- 永久免费 + 用户自带 API key
- 免费 OSS + 托管版收费
- 全付费 SaaS
- 还没想

**5. License？**
- MIT / Apache-2 / BSL / AGPL / 商业 license

> 级联：能否被竞品 fork、社区贡献门槛。

---

## Theme 2 · Bot 身份 & 认证

**6. Bot 在 GitHub 上的身份用哪种？**
- A. **GitHub App**（正式、细粒度权限、installation token、Copilot Agent 路线）
- B. **Bot user account**（一个普通用户、push 权限来自 collaborator、robobun 路线）
- C. **GitHub Action**（不需要装 App，用户自己写 workflow yaml，Claude Code Action 路线）

> 级联：决定 80% 的 auth 代码、决定 multi-tenancy 模型、决定 fork PR 是否能用。

**7. 一个 GitHub App 给所有用户用 vs 每个用户自建 App？**
- 一个共享 App（你管理 App ID / private key，用户只点 install）
- 每个用户自建 App（用户填自己的 App ID 到配置）
- 都支持（OSS 用户自建，托管版共享）

**8. 用户 OAuth token 怎么处理？**
- 不存（只用 installation token）
- 存（加密落库，让 bot 用 user 身份做事——比如 PR 评论显示是用户名而不是 bot 名）
- 二者都支持

**9. Multi-tenant 模型：一个 bot 实例服务多少个 install？**
- 单租户：每个用户自部署，只服务自己
- 多租户：一个服务跑很多 org（你做托管）
- 都支持

> 级联：决定数据库 schema（要不要 `org_id` 分区）、决定限流模型、决定 token 隔离。

**10. 自托管 vs 托管：你打算提供哪种部署模式？**
- 只 docker-compose 自托管
- 只托管 SaaS
- 两者都做（先自托管，再托管）
- 都做但 priority 是 ___

---

## Theme 3 · 通用入口 / 多 channel 抽象（你明确说要提前设计）

**11. "通用入口"是什么意思？**
- A. 一套 webhook 路由层，下面分发到不同 channel adapter
- B. 一套统一的 thread/conversation 抽象，任何 channel 都映射到它
- C. 一套统一的 LLM agent，channel 是 "shell"，agent 不知道自己在哪个 channel

> 级联：决定能不能做"Slack thread + Linear ticket + GitHub PR 关联到同一个 conversation"这种高级特性。

**12. 跨 channel 的 thread 关联怎么做？**
- 不关联，每个 channel 自成 thread
- 启发式关联（比如 GitHub PR title 包含 Linear ticket ID 就视为同一 thread）
- 显式关联（用户用命令 `@bot link slack:thread123 linear:ABC-456`）
- 都支持

**13. 状态存储模型？**
- 每个 channel 一份 state（per-channel）
- 全局统一 state，channel 只是 surface
- 折中：channel-specific cache + 全局 long-term memory

**14. Webhook 验证：每个 channel 都有自家签名方案（GitHub HMAC-SHA256、Slack signing secret、Linear OAuth、Discord Ed25519）——这套统一在哪一层做？**
- 在每个 channel adapter 内部做
- 在统一 middleware 做（adapter 提供 verify function）
- 不验证（只在内网跑，不开公网）

**15. MVP 阶段先支持哪些 channel？**
- 只 GitHub（最快出 demo）
- GitHub + Slack
- GitHub + Linear
- GitHub + 全部预留 hook

> 级联：决定抽象层在 MVP 是否真做出来。如果只支持 GitHub 就过度抽象，会浪费时间；如果一开始就要 4 个 channel，抽象一定要做。

**16. 跨 channel 的 user identity 怎么 reconcile？**（同一个人在 Slack 是 U123，在 GitHub 是 alice，怎么知道是同一人）
- 不 reconcile
- 用户在 web frontend 手动绑定
- 自动（用 email 之类的匹配）
- MVP 不做

---

## Theme 4 · 可扩展性（你说 "可拓展"）

**17. "可拓展"是给谁扩展？**
- 只给你自己（内部模块化）
- 给社区贡献者（提 PR 加 plugin）
- 给端用户（用户在 web UI / config 文件里启用 plugin）
- 给企业 partner（卖 plugin marketplace）

**18. Plugin 形式？**
- Python 模块（in-tree，重新部署）
- Python 模块（hot-load，PyPI 分发）
- MCP server（外部进程，按协议通信）
- LangGraph tool（plain function）
- 配置文件（YAML 描述行为，无代码）

> 级联：决定 plugin 隔离机制、决定能否做 marketplace。

**19. Plugin 跑在哪？**
- 主进程内（最快但有安全风险）
- 独立 sandbox（如 Daytona / Modal / E2B）
- 用户自己的 GitHub Actions runner

**20. Plugin 能访问什么 API？**
- 完整 sandbox（任意 shell 命令）
- 受限的 tool whitelist
- 仅 LLM 完成 / 评论 / label 这几个高层动作

**21. Plugin 注册 / 发现机制？**
- 在仓库 `.openbot/plugins.yaml` 列出
- 在 Web UI 上勾选启用
- 自动发现（扫描 `.openbot/plugins/*.py`）

---

## Theme 5 · 功能 1：Issue 创建后自动 triage（label / dedup / 复现 / 优先级）

**22. 触发方式？**
- 全自动（issue 一开就跑）
- 用户在 issue body 加 `@openbot triage` 才跑
- 加 label `needs-triage` 才跑
- 都支持

**23. 4 个动作的输出格式？**
- A. 一条统一评论（含 4 个 section）
- B. 4 条分别评论
- C. 直接改 issue 状态（加 label / link duplicate / 改 milestone），评论作 summary

**24. Label 从哪儿来？**
- 自动发现 repo 已有 label 集合，从里面选
- 由用户在 `.openbot/config.yaml` 显式列出可用 label
- bot 创建新 label（如果没有）
- 用 LLM 自由生成 free-form label（不推荐——会爆炸）

**25. Dedup 算法？**
- 向量嵌入 + 相似度（top-K 候选 → LLM rerank）
- 纯 LLM（喂 N 个 candidate 给 LLM 判）
- 关键词 + LLM
- 不做 dedup（MVP 跳）

**26. Dedup 的相似度阈值与输出？**
- ≥ X% 视为 dup → 自动 close + comment `Duplicate of #N`
- ≥ X% 视为 candidate → 评论 "可能与 #N 重复，请确认"，让 maintainer 决定
- 永远只 suggest，不 auto-close

> 级联：影响外部贡献者体验。bot auto-close 错了会很惹人厌。

**27. 自动复现的 scope？**
- 只对带 stack trace / "steps to reproduce" 的 issue 跑
- 所有 issue 都尝试
- 跑前先让 LLM 判断"这个 issue 能不能复现"，能再跑

**28. 复现的 sandbox 资源限制？**
- CPU / 内存 / 时长上限
- 网络访问？（要装 pip 就得开）
- 文件系统隔离强度

**29. 复现支持的语言 / runtime？**
- 只 Python（最容易）
- Python + JS/TS
- 全栈（Python/JS/TS/Go/Rust/Java/...）—— 需要每个 language toolchain 都准备好

**30. 复现成功后怎么输出？**
- 评论里贴一段失败的 test code
- 开 draft PR 含 `tests/regression/issue_N.py`
- 同时做两者

**31. 复现失败怎么办？**
- 评论 "Cannot reproduce"，给 bot 尝试过的 prompt/script
- 静默（不评论，避免污染）
- 评论 "Need more info" + bot 想知道什么

**32. 优先级评估的维度？**
- 单一维度（P0/P1/P2/P3）
- 二维（severity × effort）
- 多维（severity / scope / urgency / effort）
- 仅 free-form 推理（"this looks like a regression, suggest P1"）

**33. 优先级是 bot 直接打 label，还是 suggest？**
- 直接打
- 建议（评论里说，maintainer 决定）

---

## Theme 6 · 功能 2：PR Review

**34. 触发方式？**
- 每个新 PR 自动跑（CodeRabbit 模式）
- 仅 `@openbot review` 触发（Cursor 模式）
- 仅特定 label 触发

**35. Review 范围？**
- 只看 diff（最便宜）
- diff + 周边几行上下文
- diff + 全文件
- diff + 整个 repo（最贵，但能 catch cross-file issue）

**36. 评论形式？**
- 仅 PR top-level 摘要
- 仅 inline comment
- 摘要 + inline 都有
- formal review submission（含 changes_requested / approved / commented）

**37. 评论数量上限？**
- 不限（recall 优先）
- 单 PR 最多 N 条（precision 优先，避免 noise）
- 按 severity 阈值过滤（只发 critical/high）

**38. False positive 容忍度？**
- 高（敢说就说，错了 dismiss 就行）
- 低（必须有 X% 把握才发——参考 Cursor 的 resolution rate）

**39. 多轮对话？**
- 用户在 bot comment 下回复 → bot 再回
- 用户 @openbot 提问 → bot 答
- 不做对话，只单次 review

**40. 是否能 block merge？**
- 仅 advisory（不影响 status check）
- 可配置（默认 advisory，repo owner 可开启 blocking）
- 永远 blocking（review 不通过不让 merge）

**41. 默认开 / 关？**
- 装了 App 后默认对所有 PR 开
- 默认关，需手动 enable
- 仅对加了某 label 的 PR 开

**42. 跑的成本上限？**
- 单 PR 最多 $X
- 单 repo 每月最多 $Y
- 超限就 skip，并评论 "monthly budget exceeded"

---

## Theme 7 · 功能 3：Issue → PR autonomous fix

**43. 触发方式？**
- assign issue 给 @openbot
- 在 issue / PR 评论里 `@openbot fix`
- 加 label `try-bot` 或类似
- 全部支持

**44. 权限：谁能触发？**
- 任何人
- 仅 collaborator / write 权限以上
- 仅 OWNERS file 列出的人
- 可配置

**45. 单次任务的步骤 / 时长 / 成本上限？**
- step 上限：____
- 时长上限：____
- 成本上限：$____

**46. 分支命名约定？**
- `openbot/<issue-num>-<slug>`
- `farm/<sha>/<slug>`（robobun 风格）
- 用户可配置

**47. push 到 fork 还是主 repo？**
- 仅 fork（更安全）
- 主 repo（需要 App 装时申请 contents:write）
- 取决于触发者权限

**48. PR 状态？**
- 总是 draft
- 总是 open（让 CI 立刻跑）
- 看任务复杂度
- 用户可配置

**49. CI 失败怎么处理？**
- 等 CI 完成 → 读 log → 自己 fix → push 新 commit
- 只贴评论说 "CI failed, please investigate"
- 单次 task budget 内尝试 N 次 retry
- 不管 CI（人来处理）

**50. Bot 自己合并 PR 吗？**
- 永远不（robobun 也不合并）
- 某些情况下（如纯文档、纯依赖升级）可自动合
- 可配置

**51. 如果 bot 跑出来明显不对，怎么办？**
- 不输出，但留个 "I tried but couldn't solve this, here's what I attempted" 评论
- 直接开 PR 让人 review（信任 reviewer）
- 内部 LLM judge 先评估再决定是否开 PR

**52. 任务取消机制？**
- 用户评论 `@openbot cancel` 中断
- 用户 reaction 👎 中断
- 没有取消（agent 跑完为止）

---

## Theme 8 · 功能 4：@-mention 通用任务

**53. 命令面设计？**
- 保留词（`/review`、`/fix`、`/test`、`/explain`）+ 自由文本走 LLM
- 纯自然语言（让 LLM 决策路由）
- 混合：`@openbot <verb>` 是保留词，`@openbot <free text>` 是自由对话

**54. 列出 MVP 要支持的保留词。**
- `/review` `/fix` `/explain` `/test` `/triage` `/rebase` `/cancel` `/help` ……

**55. 触发权限？**
- 任何 commenter
- repo write 以上
- 可配置（默认 write）

**56. Rate limiting？**
- 每个用户每天最多 N 次 @mention
- 每个 repo 每月 budget
- 不限

**57. 上下文 inheritance？**
- bot 只看当前 thread 的所有评论
- bot 看 thread + linked issue/PR
- bot 看 thread + 整个 repo

**58. 失败处理？**
- 评论 error message
- 静默
- 评论 + 建议下一步

---

## Theme 9 · 非功能性

**59. 延迟目标？**
- ACK（webhook 收到 → 评论 "I'm working on it"）：5s / 10s / 30s
- 简单任务（review / triage）：1min / 5min / 15min
- 复杂任务（issue → PR）：15min / 1h / 4h

**60. 可用性 / 可靠性？**
- 99% / 99.9% / best-effort

**61. 成本目标 / 单位经济学？**
- 单 issue / 单 PR 平均 $
- 单 repo 月度 $
- 全平台月度 budget cap

**62. 观测性？**
- 必要：trace、log、cost 跟踪
- 加分：dashboard、per-repo / per-user 分桶统计
- 选用：Langfuse / LangSmith / 自建

**63. 多语言（i18n）？**
- bot 评论用什么语言？跟 issue/PR 语言走？固定中/英？
- 中文 issue 怎么处理？

---

## Theme 10 · 安全 & 滥用防护

**64. Prompt injection 防护？**
- 训练数据里加防注入 prompt
- 把 user content 包在 XML 标签里
- 用 separate LLM 做 sanitization
- 拒绝执行 user comment 里的命令

**65. Fork PR 怎么处理？**
- 默认不跑（fork PR 不可信）
- 跑但不给 secret（Copilot Agent 模式）
- 全跑

**66. Secret 泄漏防护？**
- bot 输出前用 regex / Trufflehog 扫一遍
- 限制 bot 能读的环境变量
- 禁止 bot 评论里贴 base64 / hex / URL 之外的可疑字符串

**67. 谁能改 bot 配置？**
- repo admin
- collaborator
- 任何 PR 改 `.openbot/config.yaml` 都能改（最不安全）
- PR 改配置要 admin 批准

**68. 审计 log？**
- 记录每次 bot 动作（comment / push / close）
- repo 内可见 audit log
- 仅后台可见

**69. Kill switch？**
- 全局：app config 一个开关停所有 bot 活动
- per-repo：装 App 时给一个 admin URL 可暂停
- per-thread：用户 `@openbot pause` 即可

**70. 成本失控防护？**
- 每 repo 月度 budget 硬上限
- 单 task 超 $X 自动终止
- 超限通知 admin

**71. 滥用举报机制？**
- Web UI 上有 report button
- 邮件 abuse@yourbot.dev
- 不做

---

## Theme 11 · 技术栈 & 工程

**72. 主语言？**
- Python（继承 Open SWE 的 LangGraph 栈）
- TypeScript（更多前端共享）
- Go / Rust（性能 / 部署简单）
- 混合

**73. LLM provider？**
- 只 Anthropic
- 多 vendor（OpenAI / Anthropic / Gemini / 本地）
- 用户自带 API key + bot 提供 default

**74. Sandbox provider？**
- 主：LangSmith / Modal / Daytona / E2B / Runloop / 自己 docker
- 备选支持哪几个？

**75. 状态存储？**
- Postgres（主存储）
- Redis（缓存 / 队列）
- 向量 DB（dedup 用）—— 选 Qdrant / pgvector / Pinecone？
- S3-compatible（artifact / log）

**76. 部署形态？**
- docker-compose（自托管）
- k8s helm chart
- 单 binary（all-in-one）
- Vercel / Fly.io / Render（最简）

**77. 前端技术栈（未来）？**
- Next.js / Remix / SvelteKit / Solid
- shadcn/ui / Tailwind
- 仅是 dashboard 还是有交互（如配置 plugin）

**78. CI / 测试策略？**
- 单元测试覆盖率目标
- e2e 测试用 mock GitHub 还是真 GitHub
- 用前面 eval 报告里的 benchmark 做 release gate？

**79. 文档与 onboarding？**
- README + docs site（mkdocs / docusaurus）
- 5 分钟 "Install on a test repo" 体验
- 视频 demo

**80. Versioning / Release？**
- Semantic versioning
- Feature flag 灰度
- Canary deploy

---

## Theme 12 · MVP / Out-of-scope（最关键的"不做什么"）

**81. MVP 必须有哪些？划清边界。**

**82. MVP 明确不做哪些？**（每个不做的都要说理由）

**83. v0.2 / v0.3 / v1.0 各自的目标？**

**84. 何时算 MVP 完成？**
- 跑通自动 triage 一个 demo issue
- 装到 X 个 repo 跑 Y 天
- 通过 Z benchmark 的 threshold

---

## 答题指南

回答这 80+ 题不一定要现在全答。建议顺序：
1. 先答 Theme 1（定位）+ Theme 2（bot 身份）+ Theme 4（扩展模型）的 8 题——这是地基
2. 再答 Theme 5/6/7/8（4 个功能）的 UX 细节
3. 最后答 Theme 9-11（非功能性 / 安全 / 技术栈）

每个 Theme 答完，PRD 的对应章节就能直接写。
