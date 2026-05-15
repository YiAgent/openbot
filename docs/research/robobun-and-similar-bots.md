# robobun 调研报告：项目专属维护 bot、AI coding agent、开源 bot 框架全景对比

> 调研日期：2026-05-14
> 调研对象：robobun（Bun 项目的 GitHub bot）及其同类
> 调研目的：为自研一个类 robobun 的 GitHub bot 选型与设计提供参考
> 所有事实均来自公开 GitHub 仓库、官方 docs 与可验证的 PR/comment 链接

---

## 目录

1. [TL;DR](#tldr)
2. [Part 1 · robobun 深度解析](#part-1-robobun-深度解析)
3. [Part 2 · 项目专属维护 bot 对比](#part-2-项目专属维护-bot-对比)
4. [Part 3 · AI coding agent GitHub bot 对比](#part-3-ai-coding-agent-github-bot-对比)
5. [Part 4 · 开源 GitHub bot 框架与基础设施选型](#part-4-开源-github-bot-框架与基础设施选型)
6. [Part 5 · 横向总结：设计模式、本质区别与趋势](#part-5-横向总结设计模式本质区别与趋势)
7. [Part 6 · 给你的建议：怎么搭一个类 robobun + LLM 能力的 bot](#part-6-给你的建议怎么搭一个类-robobun--llm-能力的-bot)
8. [参考链接](#参考链接)

---

## TL;DR

robobun 是 Bun（Anthropic 旗下 JavaScript runtime）在 2025 年底起重度使用的 AI 维护机器人。它**不是 GitHub App，而是一个普通 user account**，拥有 `oven-sh/bun` 仓库的 push 权限。它的可见部分（`.claude/commands/*.md` slash command 脚本 + `.github/workflows/claude-*.yml`）是开源的，跑在 `anthropics/claude-code-base-action@<sha>`、模型 `claude-opus-4-6[1m]`；它的不可见部分（接收 webhook、调度 sandbox、push `farm/<sha>/<slug>` 分支的 orchestrator）是闭源的。它至今已在 `oven-sh/bun` 仓库内 author 了约 2900+ PR、commented 8200+ 次，是该仓库的头号贡献者。

如果要复刻它，本质上是三件事的组合：
1. **`@mention`/label/issue-open 三种触发** 的 webhook 服务
2. **Claude Code（或同类 agent）+ sandbox** 跑实际工作
3. **结构化 CI 反馈 + 自动化清理（slop label / stale PR cron）** 让 bot 不会变成 PR 垃圾场

这套设计与业界趋势一致——`@mention`、PR-as-output、sandbox 化、`AGENTS.md`/`CLAUDE.md` 配置已经是事实标准。与传统项目专属 bot（bors、triagebot、Prow）的本质区别在于：传统 bot 是**确定性规则引擎**，AI agent bot 是**LLM 驱动的不可预测系统**——两者不会互相取代，而会**共存**。

---

## Part 1 · robobun 深度解析

### 1.1 它是什么 bot

**robobun 是一个普通 GitHub user account**，不是 GitHub App，不是 Probot。

| 项 | 值 |
|---|---|
| GitHub 账号 | https://github.com/robobun（id=117481402，创建于 2022-11-04） |
| `type` | `User`（不是 `Bot`，也不是 `Organization`） |
| `company` | `@oven-sh` |
| blog | bun.com |
| 公开 App 页 | https://github.com/apps/robobun → **404**（没有公开 GitHub App） |
| 占位仓库 | https://github.com/robobun/robobun（一行 README："Hey, I'm robobun, the official helper for Bun.") |
| 公开 gist | 13 个，内容形如"Bun Crash Analysis — 9,081 crashes from Discord"——bot 的分析产物外置 |

**在 `oven-sh/bun` 仓库的活动规模**：

- **2,931 个 PR** 由 robobun 创建（`is:pr author:robobun`）
- **8,245 个 issue/PR** 上有 robobun 的评论（`commenter:robobun`）
- **1,837 个 issue/PR** 有人主动 `@robobun`

这意味着自从 Bun 在 2025 年 12 月被 Anthropic 收购（[blog](https://bun.com/blog/bun-joins-anthropic)）之后，robobun 成了 bun 仓库**事实上的头号贡献者**。

### 1.2 触发方式与命令

robobun 有三套触发面，逐一列出可验证证据：

#### A. 人工 `@robobun` 触发（自然语言风格）

robobun 是 LLM agent，**不要求严格 slash 语法**，命令是自然语言。

| 调用模式 | 出现次数 | 示例 |
|---|---|---|
| `@robobun try ...` | 76 | [issue#28917](https://github.com/oven-sh/bun/issues/28917#issuecomment-4228275506)：Jarred `@robobun try with 8c27dbd160a4...` |
| `@robobun fix ...` | 421 | 各种 PR |
| `@robobun run ...` | 82 | |
| `@robobun rebase ...` | 32 | |
| `@robobun can you try again?` | 多次 | [PR#28732](https://github.com/oven-sh/bun/pull/28732#issuecomment-4170276396) |

社区成员（不只 maintainer）也可以触发：[PR#28152](https://github.com/oven-sh/bun/pull/28152#issuecomment-4201759512) `@Robobun try to add a regression test that fails with bun 1.3.11 …`

#### B. 自动触发 —— Issue/PR open hook

**Issue 开了就自动跑**：[issue#30571](https://github.com/oven-sh/bun/issues/30571#issuecomment-4433643848) — 用户 18:18 UTC 提交，robobun 22 分钟后回复 `✅ Reproduced — same root cause as #29219 / #14697 …`，没有任何人 ping。

**PR 开了就自动跑**：仓库根目录的 `.github/workflows/claude-find-issues-for-pr.yml` 在 `pull_request: opened/synchronize/reopened` 上跑 Claude Code，模型 `claude-opus-4-6[1m]`，action pin `anthropics/claude-code-base-action@98d41f9...`。这条 workflow 的评论作者是 `github-actions[bot]`，不是 robobun——这是一个**重要细节**：Bun 团队同时用两个身份发评论。

#### C. 标签/CI 桥接

每个 PR 上有一条 robobun 维护的**rolling status comment**，HTML 标记 `<!-- generated-comment id=oven-sh/bun#<PR> -->`。示例 [PR#30734](https://github.com/oven-sh/bun/pull/30734#issuecomment-4454192331)：

> ❌ @dylan-conway, your commit 674d8ecf has 2 failures in Build #54406 (All Failures: http://bun-linux2:6786/?commit=...)
> Updated 12:51 PM PT - May 14th, 2026

这条评论是**就地更新**而非新增，是 Buildkite (`bun-linux2:6786` 是 Bun LAN 内的失败仪表盘) 状态的镜像。它不是 LLM 生成的，是结构化 CI reporter 复用 robobun 身份。

### 1.3 功能范围

按 Bun 团队实际使用，robobun 干这些事：

| 类别 | 行为 | 实例 |
|---|---|---|
| **Triage / 复现** | issue 自动 reproduce，给出 `✅ Reproduced` / `Cannot reproduce`(76 次) / `Likely fixed by ...`(16 次) 三种结论，带 stack trace / 文件路径 / 故障地址 | [issue#28917](https://github.com/oven-sh/bun/issues/28917#issuecomment-4193543259) 诊断 darwin-arm64 main-thread guard page 错误 |
| **Autonomous PR 修复** | reproduce 之后开 PR。分支命名 `farm/<8-hex>/<slug>`，**直接推到 oven-sh/bun 本仓库**而非 fork。带回归测试 | [PR#30684](https://github.com/oven-sh/bun/pull/30684) "Fixes #30678"，加 `test/regression/issue/30678.test.ts` |
| **Review 回复** | 对 PR review comment 逐条回复 `Fixed in <sha>` / `Leaving as debug_assert!` / `Off-by-one, updated the comment` | PR #30730、#30728 review thread |
| **CI 诊断** | 区分 flake 和真实 failure，限制每条分支最多 retrigger 一次，跨 PR 对照判断不相关失败 | [PR#30720](https://github.com/oven-sh/bun/pull/30720#issuecomment-4453394557) "identical failure pattern appears on at least one concurrent unrelated PR (e.g. #30718, build #54360)" |
| **大型 merge 后批量 rebase** | Bun 在 2026-05-14 合并 ["Rewrite Bun in Rust" PR#30412](https://github.com/oven-sh/bun/pull/30412) 后，robobun 系统性地把所有在飞 Zig PR rebase 到 Rust | [PR#28727](https://github.com/oven-sh/bun/pull/28727#issuecomment-4454094616) |
| **Janitor** | `.github/workflows/close-stale-robobun-prs.yml` 每天 cron，关闭 robobun PR 闲置 90 天的 | 仓库内可见 |
| **AI slop 过滤** | `.github/workflows/on-slop.yml`：human 加 `slop` 标签 → 自动把 PR title 改成 "ai slop"、清空 body、关闭 | [PR#30680](https://github.com/oven-sh/bun/pull/30680) 即为实际产物 |

**robobun 不做的事**：

- **不 merge PR**——没有 MergedEvent 来自 robobun，合并仍由人类做
- 没有显式的 `/bench` 命令（性能数据散落在 triage prose 里）
- 不发 release

### 1.4 实现细节：哪些开源、哪些闭源

**开源（在 oven-sh/bun 仓库内）**：

1. **`.claude/commands/*.md`** — Claude Code slash command 的 prompt 规范：
   - `/dedupe`（对新 issue 做 dedup）
   - `/find-issues`（找当前 PR 相关 issue）
   - `/find-duplicate-prs`（找重复 PR）
   - `/upgrade-nodejs`、`/upgrade-webkit`
   - 每个文件 YAML front-matter 限定 `allowed-tools: Bash(gh issue view:*), Bash(gh search:*), ...` 白名单
   - 程序化指令格式：步骤 1/2/3，"launch 5 parallel agents"，"comment back with HTML marker `<!-- dedupe-bot:marker -->`"

2. **`.claude/settings.json` + `.claude/hooks/`** — Claude Code 自身的 hooks：`PreToolUse` 在 Bash 上跑 `pre-bash-zig-build.js`，`PostToolUse` 在 Write/Edit 上跑 `post-edit-zig-format.js`。

3. **`.github/workflows/`** 中的相关 yaml：
   - `auto-label-claude-prs.yml` — 给 robobun 作者的 PR 或 body 含 `🤖 Generated with` 水印的 PR 加 `claude` 标签
   - `claude-find-issues-for-pr.yml` — PR open 时跑 `/find-issues` + `/find-duplicate-prs`
   - `claude-dedupe-issues.yml` — issue open 时跑 `/dedupe`
   - `close-stale-robobun-prs.yml` — 定时清理
   - `on-slop.yml` — slop 标签自动关 PR

4. **`AGENTS.md` / `CLAUDE.md` / `src/CLAUDE.md`** — agent 读的项目向导文档。Bun 的 `CLAUDE.md` 甚至建议其他 Claude 会话用 `bun run pr:comments --json` 时过滤掉 robobun 自己的 CI 评论。

**闭源（在 oven-sh 和 robobun org 都找不到）**：

监听 `@robobun try` 评论、在 22 分钟内复现 issue、把分支 push 为 `farm/<sha>/<slug>` 的**orchestrator 本体**。从分支前缀 `farm/` 推测，他们有一个内部的"sandbox 农场"，但实现没公开。

**模型与 Action**：

- 模型：`ANTHROPIC_MODEL: claude-opus-4-6[1m]`（[1m] = 1M context window）
- Action pin：`anthropics/claude-code-base-action@98d41f9809750c4c96f2cd285746ecef889f14bc`（公开仓库：https://github.com/anthropics/claude-code-base-action）

### 1.5 权限与安装

- robobun 是 **`oven-sh/bun` 仓库的 collaborator with push access**——证据是 `farm/*` 分支直接 push 到 main repo，`head.repo.full_name = "oven-sh/bun"`
- **没有公开 GitHub App** — `github.com/apps/robobun` 是 404
- 主要只在 `oven-sh/bun` 活动；其他 org 仓库可能也是 collaborator，但公开 events 未见
- 谁能触发：**任何人都可以 `@robobun`**（社区成员 alii、bilby91 都触发过）；issue 开了会自动 triage 不需要权限

### 1.6 公开资料

- [bun.com/blog/bun-joins-anthropic](https://bun.com/blog/bun-joins-anthropic) — Bun 加入 Anthropic（2025-12），定位为"infrastructure powering Claude Code, Claude Agent SDK, and future AI coding products"，但未直接讲 robobun 实现
- Jarred Sumner 的 Twitter/X 和 blog 未见详细 robobun 架构介绍
- 二手信息（未能定位原始来源）：robobun 是 bun 仓库 most-merged contributor，连接到 Bun 内部 Discord，主要用于修 bug

### 1.7 robobun 的设计可复用要点

读完所有证据后，robobun 的"配方"实际上很清晰：

1. **三种触发面**：人工 `@bot`、issue/PR 自动 hook、CI 桥接
2. **同时用两个身份发评论**：bot account（robobun）做 push/PR；`github-actions[bot]` 做 PR open 时的自动评论。这是合理设计——前者偏向"人类协作"，后者偏向"机器报告"
3. **prompt 程序化**：`.claude/commands/*.md` 用 YAML front-matter + 编号步骤把 slash command 写成可审计的 spec
4. **闭环安全机制**：`on-slop.yml` + `close-stale-robobun-prs.yml` 保证 bot 输出的烂 PR 有清理路径
5. **结构化 CI 评论**：用 HTML 注释作为 idempotent marker（`<!-- generated-comment id=... -->`）支持就地更新
6. **branch prefix 命名**：`farm/<sha>/<slug>`——sandbox 实例 id + 任务 slug，便于 GC

---

## Part 2 · 项目专属维护 bot 对比

### 2.1 Rust bors / homu —— merge bot 鼻祖

仓库：[rust-lang/homu](https://github.com/rust-lang/homu)（旧 Python 版）、[rust-lang/bors](https://github.com/rust-lang/bors)（新 Rust 版）
文档：[Bors - Rust Forge](https://forge.rust-lang.org/infra/docs/bors.html)、[Help 页](https://bors.rust-lang.org/help)

**A. 交互**：PR 评论以 `@bors` 开头。常用命令：

| 命令 | 作用 |
|---|---|
| `@bors r+` / `r=user` / `r-` | 批准 / 代审 / 撤销 |
| `@bors try` / `try-` | 跑 try-build（不合并，用真实合并 SHA 跑 perf/crater） |
| `@bors p=NUMBER` | 设优先级 |
| `@bors rollup` / `rollup=never\|iffy\|maybe\|always` | rollup 资格 |
| `@bors retry` | 重新排队 |
| `@bors delegate=USER` / `delegate+` | 委派审核权（作者自审） |
| `@bors ping` | 健康检查 |

**B. 架构**：核心算法是 **"Not Rocket Science Rule"**——永远在"base + this PR"的真实合并 commit 上跑 CI，绿了才推到 default branch。

- bors 维护 `auto` 分支，把 master 推到 auto，merge PR 触发 CI，绿了 fast-forward 到 master
- Try-build 走 `try` 分支，独立 CI 配置
- Merge queue 串行（按 priority + rollup + FIFO），所以发明了 rollup：多个 `r+` 的 PR 打包成一个合并 commit 分摊 CI 时间
- CI 后端：早期 Buildbot/Travis，现在 GitHub Actions

**C. 功能**：只做**安全 merge**（try-build、merge queue、rollup、优先级）。不打标签、不分配 reviewer、不 backport、不发 release（这些是 triagebot 的活）。

**D. 权限**：`r+` 权限来自 `rust-highfive`/team 配置，本质是 reviewer team 成员；非授权用户发命令被忽略。delegate 机制允许临时让 PR 作者 `r+` 自己的 PR。bors 自己是 GitHub App，使用 installation token。

### 2.2 rust-lang/triagebot（rustbot）—— 同生态的 triage 伙伴

仓库：[rust-lang/triagebot](https://github.com/rust-lang/triagebot)
文档：[Triagebot - Rust Forge](https://forge.rust-lang.org/triagebot/index.html)、[Mastering @rustbot](https://rustc-dev-guide.rust-lang.org/rustbot.html)

**A. 交互**：`@rustbot` 前缀，部分命令兼容 `r?` 短语法。

| 命令 | 作用 |
|---|---|
| `@rustbot claim` / `release-assignment` | 自分配 / 释放 |
| `r? @user` / `r? compiler` | 分配 reviewer（自动从 review team 选） |
| `@rustbot label +T-compiler -needs-triage` | 加减标签（白名单见 `triagebot.toml`） |
| `@rustbot ping T-compiler` | ping team |
| `@rustbot ready` / `author` | 切 PR 状态（`S-waiting-on-review` ↔ `S-waiting-on-author`） |
| `@rustbot prioritize` | 提请 prioritization team |
| `@rustbot note ANCHOR Text` | 维护 PR 顶部摘要 |
| `@rustbot blocked` / `concern <text>` | 标记 FCP 阻塞 |
| `@rustbot transfer rust-lang/cargo` | 跨 repo 转 issue |

每个仓库自己的开关在 `triagebot.toml`（[rust-lang/rust 的配置](https://github.com/rust-lang/rust/blob/main/triagebot.toml)）。

**B. 架构**：Rust 实现的 webhook 服务，GitHub App。状态存 PostgreSQL（assignment、note、ping 历史）。同时接 Zulip webhook（双向同步）。

**C. 功能**：assignment、relabel、ping team、状态机、note 摘要、major change proposal、PR review rotation、Zulip 同步。**不合并 PR、不跑 CI、不发 release。**

**D. 权限**：标签白名单——非 team 成员只能加 `triagebot.toml` 明列的标签。assignment 受 review team 限制；`r? compiler` 从 team 随机选有空的人。

### 2.3 Kubernetes Prow —— 工业级标杆

仓库：[kubernetes-sigs/prow](https://github.com/kubernetes-sigs/prow)
文档：[docs.prow.k8s.io](https://docs.prow.k8s.io/docs/overview/)、[Command Help](https://prow.k8s.io/command-help)、[Tide config](https://docs.prow.k8s.io/docs/components/core/tide/config/)

**A. 交互**：斜杠命令风格 `/foo`。常用 10 条：

| 命令 | 作用 |
|---|---|
| `/lgtm` / `/lgtm cancel` | 加 `lgtm` 标签（"代码看起来 OK"） |
| `/approve` / `/approve cancel` | 加 `approved`（必须是 OWNERS approver，等同合并授权） |
| `/test <jobname>` / `/retest` | 触发特定 ProwJob 或重跑失败 |
| `/hold` / `/hold cancel` | 加 `do-not-merge/hold` 阻止 Tide |
| `/assign @user` / `/cc @user` / `/unassign` | 分配 assignee/reviewer |
| `/kind bug\|feature\|cleanup` | 加 `kind/*` |
| `/area networking\|api` | 加 `area/*` |
| `/priority critical-urgent` | 加 `priority/*` |
| `/milestone v1.30` | 设 milestone |
| `/ok-to-test` | **fork PR 的关键命令** — 授权它跑 CI |

另有 `/retitle`、`/close`、`/reopen`、`/wip`、`/release-note`、`/sig`、`/hold` 等数十个。

**B. 架构**：Go 编写，K8s 集群上的微服务群：

- **Hook** — 接 webhook 分发给 plugin
- **Plank** — 把 ProwJob CRD 翻成 K8s Pod
- **Tide** — merge queue + 自动 retest 引擎
- **Deck** — Web UI（prow.k8s.io）
- **Crier** — 把 job 状态回报到 GitHub status / Slack
- **Horologium** + **Sinker** — 周期任务 + GC

CI 跑在 K8s pod 里，每个 job 一个 Pod，天然 sandbox。

**Tide 的核心创新（区别于 bors）**：用 GitHub Search API 查"满足条件的 PR 集合"（如 `label:lgtm label:approved -label:do-not-merge/*`），打包成 **batch**，在合并目标分支上 cherry-pick 一遍跑 CI，绿了 fast-forward。**并行 batch 而不是串行**。

**C. 功能**：Merge queue（Tide）+ ChatOps + 大规模 CI 调度 + 状态 UI + 周期任务——一站式 CI/CD 平台，不止 bot。

**D. 权限**：**OWNERS 文件**是核心。每个目录可以有 `OWNERS`，列 `approvers:` / `reviewers:`。

- `/lgtm`：任何 reviewer 都能给
- `/approve`：必须是 OWNERS approver，且必须覆盖 PR 所有修改文件的 OWNERS 路径

**fork PR 安全**：默认不自动跑 CI，需要 trigger 权限的人评论 `/ok-to-test`（防止外部 PR 偷 secret）。token 用 GitHub App + bot account（k8s-ci-robot）混用。Job 在独立 K8s pod 里跑，namespace 隔离。

### 2.4 Chromium Gerrit + CQ / LUCI（简略）

Chromium 不在 GitHub。触发靠 **label vote**：

- `Commit-Queue+1`（"CQ Dry Run"）— 跑 try bot 不合并
- `Commit-Queue+2`（"Submit to CQ"）— 跑完绿了 submit
- `Code-Review+1/+2` — 人工审

LUCI Change Verifier（CV）是新版 CQ（Go），监听 Gerrit 事件 → 调度 LUCI builder 集群（buildbucket、swarming），每个 try job 在隔离 VM/container 里跑。整套基础设施是 Google 内部 production 系统的开源版。

### 2.5 PyTorch pytorchbot / pytorchmergebot

源码：[pytorch/test-infra/torchci](https://github.com/pytorch/test-infra/tree/main/torchci)；文档：[Bot commands wiki](https://github.com/pytorch/pytorch/wiki/Bot-commands)

**A. 交互**：`@pytorchbot <subcommand>` 风格（argparse 式 CLI）：

| 命令 | 作用 |
|---|---|
| `@pytorchbot merge` | 等所有必需 check 绿后合并；`--force` 跳过非必需；`-i`/`--ignore-current` 忽略当前失败 |
| `@pytorchbot revert -m "reason" -c nosignal` | 回滚（限 Meta 员工，`-c` 给回滚分类） |
| `@pytorchbot rebase --branch viable/strict` | 强制 rebase |
| `@pytorchbot label "module: distributed"` | 加标签 |
| `@pytorchbot drci` | 刷新 PR 描述里 Dr.CI 的失败分类段落 |
| `@pytorchbot help` | 帮助 |

**B. 架构**：Node.js + TypeScript，基于 **[Probot](https://probot.github.io/)**，部署到 **Vercel**（HUD 也是同一个 Next.js app）。AWS Lambda 跑非幂等的后台任务；ClickHouse 存 CI 历史（之前是 Rockset）。Merge 实操：调 GitHub API + GHA workflow `trymerge.yml`，在 GHA runner 里 cherry-pick/rebase/push 到主分支。"merge rules" 写在 [`.github/merge_rules.json`](https://github.com/pytorch/pytorch/blob/main/.github/merge_rules.json)。

**C. 功能**：merge / revert / rebase / label / Dr.CI 状态生成 + 自动 label + 同步 fbcode 内部 diff。不做 issue triage（用别的小 bot + GHA）。

**D. 权限**：`merge` 需要 `merge_rules.json` 匹配 + PR 被批准；`revert` 限 Meta 员工（GitHub team membership 判定）；fork PR 需要 `ciflow/*` 标签或 reviewer 评论触发 CI。

### 2.6 Node.js nodejs/github-bot

仓库：[nodejs/github-bot](https://github.com/nodejs/github-bot)
配套：[node-core-utils 的 `git node` CLI](https://nodejs.github.io/node-core-utils/docs/git-node.html)

**A. 交互**：**标签驱动**，命令很少：

- 加 `request-ci` 标签 → 触发 Jenkins `node-test-pull-request` job，结果回贴 status check
- `backport-open-vX.x` / `backported-to-vX.x` / `dont-land-on-vX.x` / `baking-for-lts` — backport 状态流转标签
- PR 描述的元数据行（`PR-URL:`, `Reviewed-By:`, `Fixes:`）— bot 校验格式
- 自动 ping `@nodejs/<subsystem>` team（基于 PR 修改的文件路径，映射在仓库里维护）

**B. 架构**：Node.js 服务，**不用 Probot**，直接监听 GitHub webhook（HTTP server），单实例服务整个 nodejs org。每个 event type 一个脚本。用 `JENKINS_API_CREDENTIALS` 远程 trigger Jenkins。Jenkins worker IP 白名单写在 bot 配置里用于反向校验 status 回调。

**实际的 merge 是 collaborator 用 [`git node land`](https://nodejs.github.io/node-core-utils/docs/git-node.html) 本地命令做的**——bot 不替合并。backport 用 `git node backport`。

**C. 功能**：CI 桥接（GitHub ↔ Jenkins）、元数据校验、子系统 ping、标签自动化、backport 状态机。不自己 merge / cherry-pick，不跑代码、无 sandbox。

**D. 权限**：`request-ci` 必须 collaborator 加（依赖 GitHub 写权限限制）；Jenkins worker IP 白名单防伪造 status；单一 bot account 用 PAT/App token；fork PR 需 collaborator 加 `request-ci`。

### 2.7 LLVM llvmbot

文档：[LLVM GitHub User Guide](https://llvm.org/docs/GitHub.html)

**A. 交互**：标签 + issue 评论触发的 GHA workflow：

- `/cherry-pick <commit-sha> [<sha2> ...]` — 自动 cherry-pick 到 `release/Xx` 并开 backport PR
- `release:backport` 标签 — 标记请求
- 子项目相关 label 由 workflow 根据路径自动加（`backend:AArch64`、`clang` 等）
- 自动 reviewer ping：基于 path → team 映射

**B. 架构**：一组 **GHA workflow** + llvmbot 这个 bot 账号 + 单独的 **LLVM Buildbot 集群**（pre/post-commit CI，Python，配置在 `zorg` repo）。pre-commit CI 走 Buildkite。

**C. 功能**：backport workflow、自动 label/ping reviewer、pre-commit CI 调度、post-commit 持续构建。不实现 merge queue（LLVM 依赖人手 squash-and-merge）。

**D. 权限**：`/cherry-pick` 任何人都能触发（PR 开了还要 reviewer 审核合并）；fork PR 的 buildkite job 在隔离 worker 上跑、不挂 secret；llvmbot 用 PAT 做 push。

### 2.8 项目专属 bot 共性总结

7 个 bot 跨越十几年，但模式高度收敛：

1. **触发面是 PR/Issue 评论或 label 变化**，命令以固定前缀（`@bors` / `@rustbot` / `@pytorchbot` / `/lgtm`）开头，便于正则解析；**很少用自然语言**
2. **命令到动作一对一确定性 FSM**——每个命令几十到几百行逻辑，**没有 LLM 决策**。这意味着可测、可回放、可审计
3. **权限模型外置**：依赖 GitHub team / OWNERS 文件 / 标签白名单，bot 自己几乎不存权限表
4. **fork PR 普遍采用"显式 trust label 才跑 CI"模式**（Prow 的 `/ok-to-test`、PyTorch 的 `ciflow/*`、Node 的 `request-ci`）防 secret 泄漏
5. **Merge 安全靠"在合并 SHA 上跑 CI"**：bors 的 staging 分支、Tide 的 batch、CQ 的 try job、PyTorch 的 GHA `trymerge`，本质都是 "Not Rocket Science Rule" 的工程化
6. **架构上是无状态 webhook 服务 + 外部 CI 集群**：bot 进程只做"接收事件→改 label / 触发 job / 发 API 调用"，重活外包给 Jenkins / Buildkite / GHA / K8s Pod
7. **token 几乎都用 GitHub App installation token**（少数老项目用 bot account PAT），不存终端用户 OAuth
8. **职责高度收敛**：bors 不打标签，triagebot 不合并；每个 bot 只负责一两类自动化，互相组合而不重叠

---

## Part 3 · AI coding agent GitHub bot 对比

### 3.1 Sweep AI

**现状提示**：Sweep 的 GitHub bot 模式已于 2024 年中后期事实上停止维护，团队 pivot 到 JetBrains IDE 插件。下面是巅峰期设计。

**A. 交互**：issue title 加 `Sweep:` 前缀，或给 issue 打 `sweep` label 触发。PR 上对 Sweep comment 回复算 multi-turn 指令。

**B. 架构**：GitHub App，跑在 Sweep 自己的云（也提供自托管）。**全量 clone repo + vector index**（chunking + embeddings + ranking）做 RAG，因为 issue 描述往往不指明文件。LLM 早期 GPT-4，后期 DeepSeek / Anthropic 混用。用 GitHub App installation token push 到 `sweep/` 前缀分支。

**C. 功能**：issue → PR（autonomous）+ multi-turn 修订。能读 GHA 失败 log 自动 retry。

**D. 权限**：装 App 时 Contents/Issues/PR 写权限。触发权限取决于谁能加 label（默认 repo write），但 `Sweep:` 前缀任何 issue creator 都能加——早期被滥用。计费 Free + Pro($120/mo) + Enterprise，已停 SaaS 新签。

**对你的启发**：Sweep 失败的教训是"太通用 + 缺乏 deep customization hook"，最终敌不过 Copilot Agent；项目专属 bot 反而活得更好。RAG-on-codebase 是早期方案，今天的 deep agent 更倾向"让 agent 自己 grep"——你已经在更现代的路线上。

### 3.2 CodeRabbit

**A. 交互**：装好 GitHub App 后**默认自动**对每个 PR 跑 summary + line-by-line review。PR 评论里支持斜杠 @mention：

- `@coderabbitai review` — 触发完整重新 review
- `@coderabbitai resolve` — 关闭自己的 comment
- `@coderabbitai explain` — 解释代码
- `@coderabbitai generate docstring` — 生成 docstring
- `@coderabbitai help` — 列出全部

还可以直接对它任何一条 comment **自然语言回复**展开对话，由 LLM 自己解释意图。

**B. 架构**：GitHub App + 厂商云（也支持 GitLab/Azure DevOps/Bitbucket）。读 PR diff + 必要的上下文文件（**不全 repo 索引**，比 Sweep 轻量）。多模型路由（Claude/GPT/自训），加 40+ linter 工具输出作为 context。

**C. 功能**：主线 PR review（line-by-line + summary）。auto-fix 建议（PR 上 "Apply suggestion" 按钮）。能发 Jira/Linear/GitHub issue。multi-turn 对话。学习团队 review 风格。

**D. 权限**：GitHub App 要 Contents 读 + PR 读写 + Issues 读写。fork PR：CodeRabbit 自己 host，不受 secret 限制（自带 LLM API key）。**OSS 免费 forever**，私有 $15/user/mo。

**对你的启发**：CodeRabbit 是"PR review-only"最干净的范式——默认自动跑、@mention 加细化命令、回复 comment 开对话——已经是事实标准。OSS 免费撬装机量做数据飞轮。

### 3.3 Cursor BugBot

**A. 交互**：在 Cursor Dashboard 连 GitHub/GitLab，开 BugBot 开关。默认对每个新 PR 自动 review。`cursor review` 或 `bugbot run` 评论改成只在 @mention 时跑。它的 comment 末尾有"Fix in Cursor"按钮，跳到 Cursor IDE 的 **Background Agent**（云端 agent），由它改完直接 push 到该 PR。

**B. 架构**：GitHub App + Cursor 云。BugBot 本身只做"检测 + 跳转入口"；autofix 通过 Background Agent 闭环。`.cursor/BUGBOT.md` 文件让 repo 内嵌定制规则。

**C. 功能**：主线 PR bug detection，不主打 review summary。autofix 通过 Background Agent。声称 70%+ 的 flag 在 merge 前会被 resolve。

**D. 权限**：GitHub App 写权限（Background Agent 写代码用）。任何 PR 都自动跑，触发命令要 repo write。**2026-5 改成 usage-based 计费**（按 token / agent runtime）。

**对你的启发**：BugBot 把"检测 bot"和"修复 agent"**解耦**——bot 给入口，重活留给 Background Agent。但代价是用户离开 GitHub 跳 Cursor。你 Open SWE 的设计是"bot + agent 同一个 LangGraph 在 GitHub 闭环"，对 OSS 更友好。

### 3.4 GitHub Copilot Coding Agent（`@copilot`）

**A. 交互**：**把 Issue assignee 设成 `@copilot`** —— 核心 UX，跟 assign 人一样。PR 评论里 `@copilot ...` 让它继续改。github.com / GitHub Mobile / `gh` CLI 都能 assign。

例：开 issue → assignees 选 Copilot → Copilot 自动开 draft PR → PR review 留 comment `@copilot fix the failing test` → 它再 push commit。

**B. 架构**：跑在 **GitHub Actions runner**（你的 repo 内），由 GitHub 后台自动调度，**不需要你写 workflow**。一个特殊的 **sandboxed dev environment**——MCP-driven，受限网络（你能 allowlist 域名）。用户可写 `.github/workflows/copilot-setup-steps.yml`（必须含 `copilot-setup-steps` job）自定义环境（装依赖、预 build、装工具）。

底层 OpenAI 模型 + GitHub 内部 agent loop。commit/push 用 `copilot-swe-agent[bot]` GitHub App token，push 到 `copilot/*` 前缀分支。

**重点安全**：agent runs are treated as **untrusted**（类似 fork PR）—— **org secret 不暴露**给 Copilot 触发的 workflow run。需要在 repo 级单独配置 secret。

**C. 功能**：issue → PR（完整路径）：plan → branch → code → run tests → review request → revise loop。拒绝跑：push 到 default branch、改 workflow file 等高危操作。multi-turn 通过 PR 评论。

**D. 权限**：触发权限：**只对 repo write+ 用户 @copilot 生效**——非 collaborator 评论它不响应。计费随 Copilot 订阅。它创建的 PR 在 CI 跑前需要人工 approve（防 prompt injection 滥用 CI）。

**对你的启发**：Copilot Agent 的"**assign 给 @copilot**"是个对 PM/manager 极其直觉的 UX——比 `Sweep:` prefix 更自然。**它的 sandbox + "Copilot run 视为 untrusted（无 org secret）"是设计自研 bot 时必须复刻的安全模型**。`copilot-setup-steps.yml` 这种"让用户自定义 agent 环境"的 hook 比硬塞 Dockerfile 友好得多。

### 3.5 Claude Code Action（anthropics/claude-code-action）

**A. 交互**：**GitHub Action**（不是 GitHub App，需要用户写 workflow yaml）。默认触发短语 `@claude`，可通过 `trigger_phrase` input 改。

自动检测两种模式：
- **Interactive**：响应 issue/PR 评论里的 `@claude`、issue assigned to Claude
- **Automation**：workflow 用 explicit prompt 跑（例如"每周扫一遍 deps"）

例：issue 里写 `@claude implement this with tests` → Action 启动 → Claude Code 在 runner 里 clone repo、写代码、commit、push 一个分支 → 开 PR → review，再评论 `@claude rebase on main` 继续。

**B. 架构**：跑在 **GitHub Actions runner**（用户的 minutes 配额）。Claude Code CLI 在 runner 里跑，有完整 shell。用 GitHub App（Anthropic 注册的 "Claude" app）+ OIDC token 拿 GitHub 写权限；workflow 需 `id-token: write` + `contents: write` + `pull-requests: write`。

也支持 OAuth（Pro/Max）或 PAT。LLM：Anthropic API / Bedrock / Vertex / Foundry 多家可选。**没有额外 sandbox VM** —— GitHub-hosted runner 本身就是隔离临时容器，结束销毁。

**C. 功能**：回答 issue/PR 提问、写代码、review、自动化任务。multi-turn 但没有跨 run 持久化 state（runner 是 ephemeral 的）。

**D. 权限**：触发由你 workflow 的 `on:` 决定。fork PR 默认不跑（secret 不暴露给 fork workflow）。token：Anthropic API key 存 repo secret；GitHub 操作用 GitHub App + OIDC 换 token。

**对你的启发**：用 GitHub Action 而非 GitHub App 是另一条路线——优点：用户的代码完全不离开他们的 infra；缺点：没法做 cross-repo 长期 state、没法做 webhook fan-out 到 Slack/Linear。你的 Open SWE 用 GitHub App + 自己 host 的 LangGraph，更接近 Copilot Agent 模型，能做 multi-channel——这是关键架构分叉。

### 3.6 OpenAI Codex Cloud / ChatGPT Codex（`@codex`）

**A. 交互**：多入口：ChatGPT、Codex CLI、ChatGPT Desktop、**GitHub PR/issue 评论 `@codex`**。

- `@codex review` — 触发代码 review
- `@codex` + 任意其他 prompt（`@codex fix it`, `@codex add tests`）—— 触发一个 **cloud task**，以 PR 为 context 开始干活并 push

仓库级开关在 Codex 设置里勾"Code review on for this repo"。

**B. 架构**：GitHub App + OpenAI 云。agent 跑在 OpenAI 自己的 sandbox（不在用户 runner 里）。模型：codex-1（基于 o3）→ **GPT-5-Codex**（2025-9 起）。`AGENTS.md` 做 per-repo / per-folder 配置（review guidelines、build cmd 等），这个文件已被 Anthropic、Cursor 等普遍参考。

**C. 功能**：PR review、PR fix、issue→PR、问 codebase。**并行多任务**（声称强项）。也有 `openai/codex-action` 给想自 host 的人。

**D. 权限**：GitHub App 装时 repo 读写。触发要 repo write + 仓库在 Codex 设置 enable。fork PR：OpenAI 云端跑，不依赖用户 secret。

**对你的启发**：Codex 把 `AGENTS.md` 立为标准。**`@codex review` vs `@codex <anything else>` 双语义**——一个保留命令做 review，剩下全是开放对话——你的 bot 可以学这个 `@bot <verb>` pattern。

### 3.7 Devin（Cognition AI）

**A. 交互**：多入口：app.devin.ai web UI、Slack（`@Devin ...`）、Linear（tag Devin on a ticket）、GitHub。**Slack/Linear 是主流**。

例：Linear ticket → tag @Devin → Devin 索引 codebase、planning、跑测试、提 PR → 在 ticket 上 comment PR 链接。

**B. 架构**：Cognition 云，每个 session 一个 **dedicated Linux sandbox**：shell + 代码编辑器 + 浏览器 + 文件系统 + 网络。能装 pip/npm/apt 包、跑浏览器查 Stack Overflow。GitHub 集成是 GitHub App。Repo "knowledge"：先索引一次，配合 build/test cmd 配置——更像 Sweep 的索引派，但加了 shell 探索。

**C. 功能**：大而全：issue→PR、PR review、bug fix、文档、迁移、跨 repo 工作。多 session 并行。有 "Devin Search"/"Devin Wiki" 附属产品。

**D. 权限**：Slack/Linear/GitHub 各自 OAuth 装。计费：**ACU**（Agent Compute Unit），1 ACU ≈ 15 分钟 active work。Core $20（~9 ACU），Teams $500/mo 含 250 ACU。

**对你的启发**：Devin 是"**Slack 是 primary，GitHub 是 output**"最坚决的设计——这正是 Open SWE 走的路（webapp.py 处理 Slack/Linear/GitHub webhooks，thread_id 路由回同一个 agent run）。ACU 计费明确告诉用户"agent 是真的在花算力"，比 seat-based 透明。

### 3.8 OpenHands（All-Hands-AI/OpenHands）

**A. 交互**：主流 **GitHub Action 部署**。触发：

- 给 issue 或 PR 加 `fix-me` label → 全面处理
- 在 issue/PR 评论里以 `@openhands-agent` 开头 → 针对这条 comment 处理

**B. 架构**：GitHub Action + 用户自带 LLM API key（OpenAI/Anthropic/本地）。`.github/workflows/openhands-resolver.yml`。需要 repo secret 配 `PAT_TOKEN`、`LLM_API_KEY`、`LLM_MODEL`。agent 在 Actions runner 里跑 OpenHands runtime（docker-based）。LLM agnostic。

**C. 功能**：issue→PR、PR fix、PR comment 修复。一次 run 一个目标，要 multi-turn 就再 comment 一次。

**D. 权限**：用户 PAT_TOKEN 决定权限——这是 OpenHands 的安全风险（PAT 通常很泛，比 GitHub App fine-grained 差）。

**对你的启发**：OpenHands 用 **label + @mention 双触发** 完全对齐 Sweep 范式，证明已是 OSS 事实标准。但用 PAT 是个明显安全短板——你的 Open SWE 用 GitHub App installation token + 加密存 user OAuth（看 `agent/encryption.py`）已更先进。

### 3.9 SWE-agent / SWE-Kit（princeton-nlp）

**不是 GitHub bot**。CLI tool — `sweagent run --problem_statement.url=<github issue url> ...`。学术派，主要用于 SWE-bench 跑分。`mini-swe-agent` 是 100 行极简版。

默认在 **Docker sandbox** 里跑。核心架构：agent loop（ReAct）+ ACI（Agent-Computer Interface，专为 LM 优化的 editor/grep 命令）。用 SWE-ReX runtime 做 sandbox 抽象。LLM-agnostic。没有 GitHub App，也没有 commit/push 自动化——输出 patch，你自己 apply。

**对你的启发**：SWE-agent 的真正贡献是 **ACI 思想**——LM 调用工具的接口要按 LM 的认知特性设计（`cat -n` 而不是 `cat` 等等）。可以参考它怎么设计 `read_file`/`execute`/`grep` 接口让 LM 用得更顺。

### 3.10 Aider 的 GitHub mode

Aider 本身是终端 pair-programming CLI（你本地跑 `aider`），**没有官方 GitHub bot**。社区方案 `mirrajabi/aider-github-action`：

- 触发：给 issue 加 `aider` label
- workflow `on: issues, types: [labeled]` + `if: github.event.label.name == 'aider'`

100% **GitHub Action**，运行在用户 runner。没有持久 service/sandbox。LLM：用户 `OPENAI_API_KEY` 等 secret。commit/push 用 Action 自带 `GITHUB_TOKEN`。aider 本身有 repo map（lightweight code summary）替代 RAG。

**对你的启发**：Aider 的 GitHub mode 是"**社区自发包装**"的例子——核心 CLI 没动，外面包 Action 就能上 GitHub。这说明只要 agent 是好用的 CLI，GitHub 集成是平凡的封装层；**Open SWE 的核心价值在 LangGraph + middleware + multi-channel routing，不在"能跑代码"本身**——后者 aider/SWE-agent 都做得很好。

### 3.11 AI agent 共性与趋势

**10 个 bot 的最大公约数**：

1. **@mention / slash command 已是事实标准**：`@coderabbitai`, `cursor review`, `@copilot`, `@claude`, `@codex`, `@openhands-agent`, `@Devin`——几乎都收敛到 `@bot-name [verb]`
2. **Issue → PR + PR review** 是两个核心 use case
3. **Sandbox 化**：除了纯 review bot（CodeRabbit），所有写代码的 bot 都跑在隔离环境里
4. **PR-as-output**：agent 最终交付物永远是"一个 PR"
5. **Multi-turn 通过 comment 实现**：状态隐式存在 thread 历史
6. **`AGENTS.md` / `BUGBOT.md` / `CLAUDE.md` / `copilot-setup-steps.yml`** 已成标准——repo 内嵌的 agent 配置文件

---

## Part 4 · 开源 GitHub bot 框架与基础设施选型

### 4.1 Probot

**是什么**：Node.js/TypeScript 写的 GitHub App 框架。把 webhook 接收、签名验证、JWT、installation token、event 路由封装成 `app.on("issues.opened", ...)` 回调风格。

**适合场景**：triage bot、PR 评论 bot、贴 label、CI 触发器、低频 ChatOps。**不适合长跑 AI agent**（无内置队列）。

**关键能力**：webhook + 签名校验、JWT 自动签发、installation token 自动缓存与刷新、`probot/smee-client` 本地隧道、内置 logger（pino）、`context.octokit` 注入。无队列、无状态层。

**入门**：`create-probot-app` 脚手架 5 分钟跑通 Hello World。

**现状**：v14.3.2，约 9.5K star，活跃但节奏放缓——核心 API 稳定多年，主要跟随 Octokit / Node LTS 升级。

**局限**：
1. 只覆盖单进程 webhook handler，没有任务队列概念，长任务必须自己外接 BullMQ/SQS
2. 插件生态（早年 `probot/stale`、`wip`）大部分已被 GitHub 官方功能或 Renovate/Dependabot 取代，"plugin 生态"实际已萎缩
3. Node 生态绑定

### 4.2 Octokit（`@octokit/*`）

**是什么**：GitHub 官方维护的 SDK 矩阵（JS/Ruby/.NET）。Python 没有官方 octokit。

**关键能力**：`@octokit/rest`、`@octokit/graphql`、`@octokit/auth-app`（GitHub App JWT/installation 流）、`@octokit/webhooks`（签名校验+类型化 event）、`@octokit/app`、`@octokit/action`。可插拔 plugin。

**现状**：octokit.js v5.0.5（2025-11），903+ 下游包。**Probot = Octokit + webhook server + event router**。

**局限**：只是 SDK，不是 bot 框架。

### 4.3 PyGithub vs github3.py

| | PyGithub | github3.py |
|---|---|---|
| star | ~7.7K | ~1.1K |
| 最新版 | 2.9.1（2026-04） | 维护节奏慢 |
| 主流度 | **明显占优** | 老牌但边缘 |

**结论**：Python 默认选 **PyGithub**。两者都只是 SDK，**都没有 webhook server**——要自己写 FastAPI/Flask。

### 4.4 go-github (google/go-github)

Google 维护的 Go REST 客户端，事实标准。完整 REST 覆盖、`go-github/v75`（2026-02）、支持 GitHub App auth（搭 `bradleyfalzon/ghinstallation`）。

webhook server 要自己写（社区有 `go-playground/webhooks`）。

### 4.5 GitHub Actions 作为 bot 平台

**是什么**：用 `on: issue_comment` / `on: pull_request_review_comment` 把 workflow 当 bot 用，`actions/github-script` 在 JS inline 调 API。

**适合场景**：仓库内部小规模 bot（label、greeting、轻量 lint）、AI Code Review、PR 内 `@claude` 触发的代码生成（Anthropic 官方就走这条）。

**关键能力**：webhook 由 GitHub 自己路由（不维护服务器）、`GITHUB_TOKEN` 自动签发（仓库级 1k/h，企业 15k/h）、secrets 管理、runner 即沙箱。

**入门**：5 分钟。

**局限**：
- **`GITHUB_TOKEN` 触发的 event 不会再触发新 workflow**（防递归）——需要 PAT 或 GitHub App token 才能让 bot 之间链式工作
- 启动 10–30s 冷启动，**不适合"对话式"低延迟交互**
- 跨仓库部署要复制 yaml 或用 organization workflows，**装机量大就难管理**
- fork PR secret 默认不可见，AI 类 bot 要用 `pull_request_target` + 严格防注入
- 6 小时单 job 上限

### 4.6 GitHub App 原生开发（不用框架）

**步骤**：(1) 注册 App 拿 App ID + private key + webhook secret；(2) 收 webhook → 校验 `X-Hub-Signature-256`；(3) 拿 payload 里 `installation.id` → 签 JWT (RS256) → POST `/app/installations/{id}/access_tokens` → 拿 installation token → 调 API。

**关键 docs**：
- JWT 生成：`docs.github.com/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app`
- Installation token：同目录 `generating-an-installation-access-token-for-a-github-app`
- Webhook 校验：`docs.github.com/webhooks/using-webhooks/validating-webhook-deliveries`（用 `hmac.compare_digest`，**禁止用 `==`**）

**入门**：一两天最小 demo，一周到生产质量。Node 团队写到一半会发现自己在复刻 Probot。

### 4.7 octomachinery (Python)

Python 3.7+ 异步 GitHub App 框架，可以理解为"Python 版 Probot"。

**现状**：**实质半弃坑**。最新 release 仍是 `0.3.x` dev 系列，最后一次 release 在 2024 年，2025-01 后 commit 极少。Snyk 标"sustainable but low activity"。**不推荐新项目采用**。

Python 团队现在更常见的路：**FastAPI + PyGithub + 自己写 JWT**，或者直接 GitHub Actions。

### 4.8 Slash-command 辅助库

- **`peter-evans/slash-command-dispatch`**（GHA，2k+ star，活跃）：监听 `issue_comment`，把 `/deploy arg` 转成 `repository_dispatch`，由另一个 workflow 处理。**ChatOps 事实标准**
- **`xt0rted/slash-commands`**：解析 + reaction + 权限校验
- **自写**：comment body 用正则 `/^@bot\s+(\w+)\s*(.*)$/` 拆词，10 行代码

LLM bot 现在更倾向 NL 触发（`@claude 帮我修这个 bug`），slash 只做强类型动作（`/approve`, `/retry`）。

### 4.9 自建 webhook 服务的常见架构

```
[GitHub Webhook]
      │  (HTTPS POST, X-Hub-Signature-256)
      ▼
[FastAPI / Express ingress]   ← 必须 < 10s 返回 2xx
      │  立即入队、返回 202
      ▼
[Broker: Redis / RabbitMQ / SQS]
      ▼
[Worker: Celery / BullMQ / Temporal / RQ]
      │  长任务、调 LLM、跑 sandbox、push commit
      ▼
[GitHub API + Slack/Linear]
```

要点：
- **ingress 必须秒级 ACK**——GitHub 重投递机制会在超时后重发
- 用 `delivery_id` 做幂等键
- Worker 长任务要心跳/续约 installation token（1 小时过期）
- **Temporal/Inngest 比 Celery 更适合 AI agent**（重试、断点续跑、long-running workflow 原生支持）

参考实现：`langchain-ai/open-swe`（本仓库）即用 LangGraph 替代 Celery，sandbox 替代 docker worker。

### 4.10 GitHub App vs Actions vs OAuth App 对比矩阵

| 维度 | GitHub App | GitHub Actions | OAuth App |
|---|---|---|---|
| **限流（标准）** | 5k/h，按 repo/user 加成至 12.5k/h | **1k/h per repo** | 5k/h per user |
| **限流（GHEC）** | 15k/h | 15k/h per repo | 15k/h |
| **权限粒度** | 细粒度 scope，仓库级 install | workflow `permissions:` 字段，仓库级 | 用户级 scope，粗 |
| **UX** | "Install App"，企业可装 org-wide | 复制 yaml 到每个仓库 | OAuth flow |
| **计费** | 自己掏服务器 | runner 分钟数计费 | 自己掏 |
| **Fork PR** | ✅ 完整 | ⚠️ 默认 secret 不可见 | N/A |
| **私有 repo** | ✅ | ✅ | ✅ 但需用户授权 |
| **冷启动** | 你自己控制（秒级） | 10–30s | 即时 |
| **长任务** | 自由 | 6h 上限 | N/A |
| **运维成本** | 高 | **零** | 中 |
| **代表场景** | Renovate、Dependabot、Open SWE | Claude Code Action、AI Code Review | CLI 工具（gh） |

---

## Part 5 · 横向总结：设计模式、本质区别与趋势

### 5.1 三类 bot 的本质区别

| 维度 | 项目专属 bot（robobun pre-AI / bors / Prow） | AI agent bot（Devin / Copilot / 现 robobun） |
|---|---|---|
| **指令** | 严格 slash 命令 `/bench`、`@bors r+` | 自然语言 `@bot 改一下…` |
| **能力边界** | 窄但深——`/bench` 永远跑同一套，可重现 | 通用——理论上能做任何 codemod，但**不可预测** |
| **失败模式** | 命令出错有明确报错；不会无中生有 | 改坏代码、prompt injection、API 超额 |
| **依赖** | 一个 CI worker + 几个 script | LLM + sandbox + 多 vendor，链路长 |
| **审计** | 易——命令-动作一一对应 | 难——agent reasoning 不稳定 |
| **维护** | 跟随项目代码，逻辑稳定 | 跟随 LLM 升级，模型变了 prompt 要重调 |
| **适合场景** | 重复性任务（跑测、发版、查 perf） | 探索性任务（bug fix、小 feature） |

**深层洞察**：AI agent bot 的"通用"与项目专属 bot 的"可控"是不可兼得的。robobun 这类 bot 不会被 AI 取代——它们在**自己的窄场景里更可靠**。未来更可能是**两者共存**：

- AI agent 干"实现"（bug fix、refactor）
- 专属 bot 干"执行/验证"（跑 benchmark、发布流水线、merge queue）

事实上**现在的 robobun 已经是混合体**：核心 agent 行为是 LLM 驱动的（reproduce、写代码、push PR），但配套有大量传统脚本式 workflow（slop label 清理、stale PR cron、CI 桥接评论）。这是当下最先进的范式。

### 5.2 三类 bot 共同的 14 个最佳实践

把所有调研归纳，自研 bot 该坚持的设计原则：

1. **`@mention` 是触发主入口**（不要再发明新语法）
2. **Slash 命令做强类型动作**（`/approve`, `/retry`），**自然语言做开放对话**——两者混用是事实标准
3. **PR-as-output**：所有路径汇聚到"开一个 PR"，因为这是 GitHub 协作语义的最小完整单元
4. **每个会话一个 sandbox**：除了纯文本 review bot，没人敢让 agent 在裸 runner 跑（Copilot 把 agent run 标 untrusted、不给 org secret 是这个趋势的极致）
5. **结构化 idempotent 评论**：用 HTML 注释做 marker（`<!-- bot:foo -->`）支持就地更新，避免重复发
6. **branch prefix 命名**：`bot/<sha>/<slug>` 或 `farm/<id>/<slug>`，便于 GC 和过滤
7. **AGENTS.md / CLAUDE.md** 让"项目知识"沉淀进 repo，跨 vendor 复用
8. **权限模型外置到 GitHub team / OWNERS / 标签白名单**，bot 自己别存权限表
9. **fork PR 必须显式 trust**（Prow 的 `/ok-to-test`、Copilot 的 untrusted run）防止 secret 泄漏 + prompt injection
10. **token 用 GitHub App installation token**，不要 PAT（OpenHands 是反面教材）
11. **webhook ingress 秒级 ACK + 后台 worker** 三层架构（GitHub 重投递机制要求 < 10s）
12. **delivery_id 做幂等键** 防重放
13. **闭环清理**：stale PR cron、slop label 自动关 PR、failed run 重试上限——bot 输出的烂内容必须有清理路径
14. **双身份发评论**：bot account 做"协作类"评论（PR、PR review），`github-actions[bot]` 做"机器报告类"评论（CI 状态、自动 dedup）——分清前台 vs 后台

### 5.3 当前 7 个技术趋势

1. **`@mention` 已成事实标准**——没人再发明新触发语法
2. **Sandbox 化**——除纯文本 review bot 外没人敢让 agent 在裸 runner 跑
3. **PR-as-output**——所有路径都汇聚到 PR
4. **保留词 + 自由文本的混合 slash command**（`@bot review` 是保留词，`@bot <free text>` 是自由对话）
5. **Multi-channel agent**（Slack/Linear/GitHub 同一个后端）——Devin 和 Open SWE 走在前面
6. **Repo 内嵌 agent 配置文件**（AGENTS.md / CLAUDE.md / copilot-setup-steps.yml）
7. **"轻 detector + 重 fixer"分层**（Cursor BugBot → Background Agent）开始出现，可能是下一代主流

---

## Part 6 · 给你的建议：怎么搭一个类 robobun + LLM 能力的 bot

### 6.1 三种实现路线

#### 候选 1：纯 GitHub Actions + Claude Code Action

**优点**：零运维、零服务器、官方背书、5 分钟接入、自动跟随 Anthropic 升级、`@claude` mention 开箱可用、secret 管理由 GitHub 兜底。

**缺点**：每次冷启 10–30s（对话体验差）、6h 上限、跨仓库部署靠复制 yaml、fork PR 安全模型复杂、状态只能塞 artifact / cache、`GITHUB_TOKEN` 不能链式触发。

**最适合**：1–20 人小团队，仓库数 < 50，bot 行为基本是"评论触发→生成代码→push PR"的一次性任务，不需要持久会话。

#### 候选 2：Probot + 外部 LLM 调用

**优点**：webhook/auth/token 全包；Node 生态丰富；可加 BullMQ 做异步；本地 smee 调试快；社区资料多。

**缺点**：Node 锁定；Probot 自身无队列、无 sandbox 概念，要拼装；LLM agent 状态（中间步骤、检查点）要自己存 Redis/Postgres；如果 agent 要跑代码，沙箱（Daytona/Modal/E2B）得另外接。

**最适合**：20–100 人团队，已有 Node 后端基础设施，bot 行为偏"事件驱动 + 短 LLM 调用"（如 PR 摘要、自动 triage、issue 分流），不需要长 agent loop。

#### 候选 3：自建 FastAPI webhook + LangGraph agent + sandbox + GitHub App（即本仓库 Open SWE 路线）

**优点**：完全可控、Python 生态对 LLM 最友好（LangGraph/DeepAgents/LangSmith）、能跑真正的长程 agent（多轮工具调用、检查点恢复、中途接收新消息）、沙箱隔离每个 thread、可同时支持 Slack/Linear/GitHub 三入口、token 通过代理注入不落地。

**缺点**：运维负担最重（webhook 服务、broker、worker、sandbox provider、监控、token 刷新、限流退避都要自己扛）、上线周期 2–4 周、需要专人维护。

**最适合**：>50 人工程团队、或把 bot 当独立产品交付、需要 agent 真正"长跑"（修复跨文件 bug、做 multi-step refactor、和用户对话式协作）、对延迟和定制化都有要求。

### 6.2 一句话决策

- 试水 / 个人项目 / 5 人以内 → **候选 1**
- 公司内部生产力 bot、Node 团队 → **候选 2**
- 想做有差异化的 AI 编码 agent 产品 → **候选 3**

### 6.3 如果完全照 robobun 思路自研，最小可行架构（MVP）

按 robobun 的实际配方分解，**最少要这五件东西**：

1. **GitHub App**（或 bot account + PAT，前者更安全）
   - 权限：Contents 读写、PR 读写、Issues 读写、Workflows 读
   - 订阅事件：`issue_comment`、`issues.opened`、`pull_request.opened/synchronize`、`pull_request_review_comment`
2. **Webhook ingress**（FastAPI/Express）——秒级 ACK、入队
3. **Worker + sandbox**——每个 thread 一个隔离环境跑 agent。生产推荐 Modal/Daytona/E2B/LangSmith
4. **LLM agent**——用 Claude Code、`anthropics/claude-code-base-action`、或自家 LangGraph 都行
5. **配套 GHA workflow**——清理脏 PR、标 AI slop、stale PR 关闭、CI 状态镜像评论（学 robobun 的 `on-slop.yml`、`close-stale-robobun-prs.yml`）

**与你 Open SWE 现有架构的对应**：

| robobun 概念 | Open SWE 现有实现 |
|---|---|
| bot account `robobun` | GitHub App（更安全） |
| Webhook 接收 | `agent/webapp.py`（FastAPI） |
| Sandbox 调度 | `agent/utils/sandbox.py:create_sandbox`（LangSmith/Modal/Daytona/Runloop/local） |
| Per-thread sandbox cache | `SANDBOX_BACKENDS` dict |
| `farm/<sha>/<slug>` 分支命名 | 你可以采用类似命名 |
| `.claude/commands/*.md` slash command | 可作为 Open SWE 的 prompts 模板 |
| Slop 清理 | 需要新增 |
| Stale PR cron | 需要新增 |
| 双身份发评论（bot + `github-actions[bot]`） | 当前只用一个身份，可以考虑拆分 |
| `<!-- generated-comment id=... -->` idempotent marker | 需要新增 |
| `AGENTS.md` / `CLAUDE.md` 注入 | 已有 |

### 6.4 你已经走对的关键决策

- **GitHub App + 加密 token**（`agent/encryption.py`+`utils/auth.py:resolve_github_token`）—— 比 OpenHands 的 PAT 模式安全
- **Per-thread sandbox**（LangSmith/Modal/Daytona）—— 与 Devin、Copilot Agent 同路线
- **Multi-channel routing**（Slack/Linear/GitHub 同后端，deterministic thread_id）—— Devin 和 Open SWE 是这条路上跑得最远的两个
- **LangGraph + DeepAgents**——比 ReAct loop 鲁棒，可恢复 + 中途接收新消息（`check_message_queue_before_model` middleware）

### 6.5 还差什么（建议补的）

1. **Slop / stale / dedupe 等"垃圾治理" workflow**——AI bot 不可避免会产出烂 PR，没有清理路径很快会变成 PR 垃圾场
2. **结构化 CI 镜像评论**（带 HTML marker、就地更新）——robobun 这条最值得抄
3. **AGENTS.md / CLAUDE.md 读取的契约文档化**——让接入 bot 的项目知道怎么把项目知识喂给 agent
4. **`copilot-setup-steps.yml` 等价物**——让用户能自定义 sandbox 环境（装项目依赖、跑 build 预热）
5. **untrusted run 模型**——你的 bot 如果创建了 PR，CI 跑前是否需要人工 approve？防 prompt injection 的最后一道闸
6. **保留词 + 自然语言混合命令面**——比如 `@open-swe review`、`@open-swe rebase`、`@open-swe try` 是保留词，`@open-swe <free text>` 走 agent loop。学 Codex `@codex review` 这套
7. **fork PR 显式 trust 机制**（学 Prow 的 `/ok-to-test`、Copilot 的 untrusted）

---

## 参考链接

### robobun 与 Bun

- [@robobun GitHub 账号](https://github.com/robobun)
- [robobun/robobun 占位仓库](https://github.com/robobun/robobun)
- [oven-sh/bun 的 .claude/commands](https://github.com/oven-sh/bun/tree/main/.claude/commands)
- [.github/workflows/auto-label-claude-prs.yml](https://raw.githubusercontent.com/oven-sh/bun/main/.github/workflows/auto-label-claude-prs.yml)
- [.github/workflows/claude-find-issues-for-pr.yml](https://raw.githubusercontent.com/oven-sh/bun/main/.github/workflows/claude-find-issues-for-pr.yml)
- [.github/workflows/on-slop.yml](https://raw.githubusercontent.com/oven-sh/bun/main/.github/workflows/on-slop.yml)
- [.github/workflows/close-stale-robobun-prs.yml](https://raw.githubusercontent.com/oven-sh/bun/main/.github/workflows/close-stale-robobun-prs.yml)
- [anthropics/claude-code-base-action](https://github.com/anthropics/claude-code-base-action)
- [Bun joins Anthropic](https://bun.com/blog/bun-joins-anthropic)
- 实例 PR / Issue：
  - [issue#28917 @robobun try](https://github.com/oven-sh/bun/issues/28917#issuecomment-4228275506)
  - [issue#28917 robobun 复现报告](https://github.com/oven-sh/bun/issues/28917#issuecomment-4193543259)
  - [issue#30571 自动 triage](https://github.com/oven-sh/bun/issues/30571#issuecomment-4433643848)
  - [PR#30684 robobun autonomous fix](https://github.com/oven-sh/bun/pull/30684)
  - [PR#30734 CI 状态评论](https://github.com/oven-sh/bun/pull/30734#issuecomment-4454192331)
  - [PR#30680 ai slop 自动关闭](https://github.com/oven-sh/bun/pull/30680)

### 项目专属 bot

- [rust-lang/bors](https://github.com/rust-lang/bors)、[rust-lang/homu](https://github.com/rust-lang/homu)
- [Bors - Rust Forge](https://forge.rust-lang.org/infra/docs/bors.html)
- [rust-lang/triagebot](https://github.com/rust-lang/triagebot)
- [Mastering @rustbot](https://rustc-dev-guide.rust-lang.org/rustbot.html)
- [kubernetes-sigs/prow](https://github.com/kubernetes-sigs/prow)
- [Prow Overview](https://docs.prow.k8s.io/docs/overview/)
- [Prow Command Help](https://prow.k8s.io/command-help)
- [Tide Config](https://docs.prow.k8s.io/docs/components/core/tide/config/)
- [Chromium CQ docs](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/infra/cq.md)
- [PyTorch Bot commands wiki](https://github.com/pytorch/pytorch/wiki/Bot-commands)
- [pytorch/test-infra torchci](https://github.com/pytorch/test-infra/tree/main/torchci)
- [nodejs/github-bot](https://github.com/nodejs/github-bot)
- [node-core-utils git-node CLI](https://nodejs.github.io/node-core-utils/docs/git-node.html)
- [LLVM GitHub User Guide](https://llvm.org/docs/GitHub.html)

### AI coding agent bots

- [sweepai/sweep](https://github.com/sweepai/sweep)、[docs.sweep.dev](https://docs.sweep.dev/)
- [CodeRabbit docs](https://docs.coderabbit.ai/)
- [Cursor BugBot docs](https://cursor.com/docs/bugbot)、[Cursor GitHub integration](https://cursor.com/docs/integrations/github)
- [GitHub Copilot Coding Agent](https://docs.github.com/copilot/concepts/agents/coding-agent/about-coding-agent)
- [Claude Code Action](https://github.com/anthropics/claude-code-action)、[Claude Code GHA docs](https://code.claude.com/docs/en/github-actions)
- [OpenAI Codex GitHub integration](https://developers.openai.com/codex/integrations/github)、[openai/codex](https://github.com/openai/codex)
- [Devin](https://devin.ai/)、[Devin Slack integration](https://docs.devin.ai/integrations/slack)
- [OpenHands GitHub Action](https://docs.openhands.dev/openhands/usage/run-openhands/github-action)、[All-Hands-AI/OpenHands](https://github.com/All-Hands-AI/OpenHands)
- [SWE-agent](https://github.com/SWE-agent/SWE-agent)、[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
- [Aider](https://aider.chat/)、[aider-github-action](https://github.com/mirrajabi/aider-github-action)

### 基础设施 / 框架

- [Probot](https://probot.github.io/)、[probot/probot](https://github.com/probot/probot)
- [octokit/octokit.js](https://github.com/octokit/octokit.js)
- [PyGithub](https://github.com/PyGithub/PyGithub)、[github3.py](https://github.com/sigmavirus24/github3.py)
- [google/go-github](https://github.com/google/go-github)
- [octomachinery](https://github.com/sanitizers/octomachinery)（半弃坑）
- [actions/github-script](https://github.com/actions/github-script)
- [peter-evans/slash-command-dispatch](https://github.com/peter-evans/slash-command-dispatch)
- [GitHub App rate limits](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/rate-limits-for-github-apps)
- [GitHub App JWT 生成](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
- [Generating installation access token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [Validating webhook deliveries](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)
