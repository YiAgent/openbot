# Eval Dashboard · 页面设计文档

> **Scope**：本仓库 eval 体系的**单页 HTML 趋势看板**设计稿。
> **状态**：design only，无代码。落地实现属于后续任务（不在 E0–E2 范围）。
> **上位文档**：[`openbot-eval-prd.md`](../prd/openbot-eval-prd.md) §9 / §10 / §15。
> **数据 source of truth**：[`baseline-log.md`](./baseline-log.md) + LangSmith experiments。

---

## 1. 目标与非目标

### 1.1 目标 (Goals)

| # | 目标 | 衡量 |
|---|---|---|
| G1 | 一眼看出**当前 release 是否通过 gate** | 头部红/黄/绿状态卡 |
| G2 | 看到**每个 suite 的主指标随时间走势** | suite-level 折线图 |
| G3 | 看到**最近一次 redteam 的 category 拆解**（哪类攻击仍在突破） | 分类柱状图 / 堆叠条 |
| G4 | 从任何一行 baseline entry **一键跳到 LangSmith run** 和触发 PR | 行级链接 |
| G5 | 让产品/审阅者**不打开 IDE** 就能审计 baseline 变更 | 完整 timeline 视图 |

### 1.2 非目标 (Non-goals)

- ❌ **不做实时 dashboard**。所有数据来自 append-only 文件 + LangSmith API，刷新节奏 ≤ 每次 baseline 重跑（PRD §9 触发条件）。
- ❌ **不做交互式 ad-hoc query**。深度分析交给 LangSmith UI；本页只读、面向 release gate 审阅。
- ❌ **不做 cost 仪表盘**。Cost 在 PRD §11，独立报告。
- ❌ **不做 internal-data suite**（PRD §4.0 全部 deferred）——页面预留卡片占位但显示 "deferred" 而不是空图。
- ❌ **不做登录 / 权限**。v0.1 内网静态发布；敏感字段（PR url 私有时）由生成端裁剪。

---

## 2. 用户与场景

| 角色 | 使用场景 | 关注信息 |
|---|---|---|
| **Release reviewer** | merge 前 60 秒决策："这个 release 能不能上" | gate 状态卡 + 最新 baseline diff |
| **Eval owner**（你） | 每周 retro：本周 baseline 有没有非预期 drift | suite 走势 + judge/dataset 变更注释 |
| **Product / 投资人** | 季度看 redteam 安全曲线 | redteam fail-safe% trend + category 拆解 |
| **新成员 onboarding** | 5 分钟看懂 "我们到底测了什么、当前在哪一档" | suite 矩阵 + 当前 release tag |

**Not a user**：实时 oncall（没有实时数据）、外部公开演示（数据敏感）。

---

## 3. Information Architecture

页面是**单 URL、单 scroll、四个语义段落**。不使用 SPA 路由、不使用 Tab。

```
┌─────────────────────────────────────────────────────────────┐
│ § Header — release identity + global gate verdict           │  ~120px
├─────────────────────────────────────────────────────────────┤
│ § Section A — Gate Status Strip                             │  全宽，固定高
│   G1 G2 G3 G4 G5 G6 G10  (七个 PRD 锁定的 gate)             │
├─────────────────────────────────────────────────────────────┤
│ § Section B — Suite Trends (per-suite cards)                │  自适应网格
│   review_martian  |  redteam_prompt_injection  |  swe_bench │
│   review_shadow   |  triage_internal           |  ...       │
├─────────────────────────────────────────────────────────────┤
│ § Section C — Latest Baseline Detail                        │  最近 N=5 条
│   行级 timeline：date · suite · delta · gate · trigger      │
├─────────────────────────────────────────────────────────────┤
│ § Section D — Footer · provenance & freshness               │
│   data hash · langsmith link · "generated at <ts>"          │
└─────────────────────────────────────────────────────────────┘
```

不出现的元素（明确剔除）：
- 侧边栏导航（页面只有一个滚动轴）
- "搜索 baseline" 输入框（不是 ad-hoc 工具）
- 用户头像 / 切换组织
- 通用 KPI tiles（"今日访问量"等）

---

## 4. 视觉方向 (Visual Direction)

定位：**Editorial / dashboard 混合**，参考 GitHub status page + Linear release notes 的克制感。
不要做：dark-mode 默认、glassmorphism、彩色渐变、动效堆叠。

### 4.1 Palette

按**语义**，不按装饰使用。所有色值用 oklch 表达。

| Token | 用途 | 备注 |
|---|---|---|
| `--c-surface` | 背景，oklch(98% 0 0) | 纯白偏暖 |
| `--c-surface-raised` | 卡片底，oklch(96% 0.005 250) | 极浅冷灰 |
| `--c-ink` | 主文本，oklch(20% 0 0) | 不用纯黑 |
| `--c-ink-muted` | 次级文本 / 标签 | oklch(48% 0.01 250) |
| `--c-gate-pass` | gate=pass | oklch(64% 0.16 145) 偏冷绿 |
| `--c-gate-soft` | gate=soft-warn (G1/G5) | oklch(74% 0.14 75) 暖琥珀 |
| `--c-gate-hard` | gate=hard-block (G2/G3/G6) | oklch(58% 0.20 28) 警示红 |
| `--c-trend-up` | delta > 0 | 与 `gate-pass` 同根 |
| `--c-trend-down` | delta < 0 | 与 `gate-hard` 同根 |
| `--c-deferred` | suite=DEFERRED (PRD §4.0) | oklch(70% 0 0) 中性灰，必须看起来"被冻"而不是"被忽略" |
| `--c-grid` | 图表网格线 | oklch(92% 0 0) |

**只有六个语义色**。不要再加蓝色/紫色作装饰；如果一个色没有 gate / trend / 状态语义，就不该出现。

### 4.2 Typography

只用两族字体，强配对策略：

- **Serif (display)**：`Source Serif 4` 或 `Newsreader`，用在 §Header release tag、§B 卡片标题、§C timeline 日期。
- **Sans (body & numerals)**：`Inter` 或 `Geist`，开 `font-feature-settings: "tnum"` （tabular numerals）以保证分数对齐。
- **Mono (code-like)**：`JetBrains Mono`，仅用于 git sha、experiment name、`heuristic/no-llm` 这类 ID 字段。

字号阶（rem-based）：

| Token | 用途 |
|---|---|
| `--fs-display` clamp(2.5rem, 1.5rem+2vw, 4rem) | release tag "v0.1.0-rc.3" |
| `--fs-h2` 1.5rem | section heading |
| `--fs-metric` 2.25rem | 卡片主分数（F1, fail-safe%） |
| `--fs-body` 1rem | 正文 |
| `--fs-meta` 0.8125rem | 时间戳 / git sha / 来源 |

**分数永远 tabular**，列对齐是这页的核心可读性约束。

### 4.3 Spacing & Rhythm

不要用统一 padding。Section 之间用 `clamp(4rem, 6vw, 8rem)` 大间距，卡片内部用 8 / 16 / 24 三档。Header 与 Section A 之间是 0（紧贴），暗示"状态条是 header 的延续"。

---

## 5. Section 详细设计

### 5.1 Header

```
┌─────────────────────────────────────────────────────────────┐
│  OpenBot · Eval Trends                                       │
│                                                              │
│  Release  v0.1.0-rc.3        Gate Verdict  ◉ HARD-BLOCK     │
│  2026-05-15 · git 9821a4f    1 of 7 gates failing (G6)      │
└─────────────────────────────────────────────────────────────┘
```

- 左侧：product 名 + 副标 (display serif)。
- 右侧分两列：当前 release tag + 全局 gate verdict (sans, large)。
- Verdict 三种状态：`PASS` (绿圆) / `SOFT-WARN` (琥珀三角) / `HARD-BLOCK` (红方块)。**形状也带语义**，色盲也能区分。
- Verdict 下方一句 plain-English："1 of 7 gates failing (G6)"。点击展开 §A。

### 5.2 Section A · Gate Status Strip

**七列等宽 chip 列表**，对应 PRD §9 表格（G1–G6 + G10）。每个 chip：

```
┌──────────────────┐
│ G6 · Safety      │      ← gate id + 短名
│ ──────────────   │
│ 0.958 / 1.000    │      ← 当前 / 阈值，tnum 对齐
│ ▲ +0.083         │      ← 与上次 baseline diff
│ HARD-BLOCK       │      ← 状态标签，颜色 = gate 色
└──────────────────┘
```

行为：
- hover 卡片 → 工具提示出 PRD §9 该 gate 的完整描述（"mean_fail_safe ≥ 1.0..."）。
- click → 锚跳到 §B 对应 suite 卡。
- gate **不存在数据**（如 G3 还没接 swe-bench-lite）→ 显示 "—" 而不是 0，颜色 = `--c-deferred`。

布局：宽屏 7 列；≤ 1024px 折成 2 行 (4+3)；≤ 640px 横向滚动 (`scroll-snap-type: x mandatory`)，不堆 7 行竖排。

### 5.3 Section B · Suite Trend Cards

每个 suite = 一张卡片，固定结构。grid: `repeat(auto-fill, minmax(360px, 1fr))`，不固定列数。

卡片骨架：

```
┌────────────────────────────────────────────────┐
│  review_martian                       ✅ v0.1  │  ← 标题 + 版本 chip
│  Primary: mean_f1 ≥ 0.55                       │  ← PRD §9 gate 文字
│                                                │
│   ┌──────────────────────────────────────┐    │
│   │       ╱╲     ___                      │    │  ← sparkline，y=0..1
│   │      ╱  ╲___╱   ╲___                  │    │     最近 12 个 baseline
│   │                                        │    │     悬停出 tooltip
│   └──────────────────────────────────────┘    │
│                                                │
│  Latest   0.833     ▲ +0.083   2026-05-15      │  ← metric row, tnum
│  Status   ◉ PASS    Judge  heuristic/no-llm    │
│  Dataset  martian_smoke_v1   stderr ±0.105     │
│                                                │
│  ↗ LangSmith experiment    ↗ Triggering PR     │  ← 链接，不是按钮
└────────────────────────────────────────────────┘
```

每张卡的强制内容（与 PRD §10.1 `run-level metadata` 一一对应）：
- suite 名 + suite_version
- primary metric 名称 + 阈值（写死 PRD §9）
- 当前值 + 与上次 delta（带方向箭头，色 = trend）
- 当前 gate 状态
- 当前 judge_model_id + judge_prompt_version
- 当前 dataset_version + stderr
- 两个出站链接：`langsmith_experiment` 名 → LangSmith URL；`pr_url` → GitHub PR

**sparkline 设计**：纯 SVG，无库依赖；x 轴 = baseline 序号（不是真实时间，因为 baseline 不是等距事件）；y 轴归一化到该 suite 的 metric 域 (F1 0..1 / fail-safe% 0..1)；threshold line 用 dashed `--c-gate-soft`。

**Deferred suite 卡片**（如 `triage_internal`）：所有数值区域替换为单行斜体 "Deferred — see PRD §4.0"，sparkline 区改为低饱和占位斜纹。颜色用 `--c-deferred`。不要隐藏，因为"看到它被冻住"本身是信息。

**特殊：redteam_prompt_injection 卡有第二屏**。除了上述结构，额外在卡底拼一个 category 拆解：

```
  Categories (latest)
  issue_body          ████████████████  4/4
  pr_comment          ████████████████  4/4
  code_comment        ████████████████  4/4
  fake_system_prompt  ████████████████  4/4
  secret_exfiltration ████████████░░░░  3/4   ← only category < 1.0
  tool_misuse         ████████████████  4/4
```

- 水平条形，等宽轨道 + 实色填充。
- 唯一一行未达 1.0 的类别用 `--c-trend-down` 描边突出（不只是颜色，因为可访问性）。
- 这一段直接消费 `baseline-log.md` 里 `new_score.by_category`，结构已锁定。

### 5.4 Section C · Latest Baseline Timeline

**时间线 = baseline-log.md 的可视化镜像**。最近 N=5 条，老的 "Show all ▾" 展开（默认折叠，避免页面无限滚动）。

每行：

```
┌────────────────────────────────────────────────────────────────────┐
│ 2026-05-15  ●  redteam_prompt_injection                            │
│              │   Trigger: scorer-logic   Release: interim-9821a4f  │
│              │   Score: 0.875 → 0.958  (▲ +0.083)                  │
│              │   Gate: HARD-BLOCK   Judge: heuristic/no-llm v0     │
│              │   ↗ experiment    ↗ inspect log    ↗ PR #32         │
│              │                                                      │
│              │   Notes  ┌──────────────────────────────────────┐   │
│              │          │ Re-baseline after Codex /codex       │   │
│              │          │ review of #32 surfaced three P1...   │   │
│              │          │ (truncated, click to expand)         │   │
│              │          └──────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────┘
```

设计要点：
- 左侧**真垂直时间轴**（单条 1px line + 圆点），不是 table。
- 日期用 display serif 大字；其余 sans。
- `notes` 字段是 baseline-log 里最长的 free-text，**默认折成 3 行 + 渐变 mask**，点击展开。这是页面唯一允许的"长 prose"区域。
- 三个出站链接固定顺序：experiment → inspect_log → pr_url。链接缺失（如 `pr_url: null`）→ 显示禁用样态而不是隐藏。

排序：倒序（最新在上）。

### 5.5 Section D · Footer · Provenance

最后一段，低对比度，但**必要**：

```
Source     baseline-log.md @ git 9821a4f                  Last gen  2026-05-15 18:42 UTC
           24 entries · 4 suites · 1 judge model
LangSmith  https://smith.langchain.com/o/<org>/projects/openbot-evals
PRD        docs/prd/openbot-eval-prd.md §9 Gating Policy
```

声明这页的"数据是哪个 git sha 下哪个文件生成的"——对 release audit 必不可少。
"Last gen" 不是 `now()`，是构建时刻；这一点必须写进文档以免误导成实时。

---

## 6. 数据契约 (Data Contract)

页面从两类数据源消费，**不引入第三方**。

### 6.1 主数据：`baseline-log.md` 的 YAML 段

每条 entry 的字段已在 [baseline-log.md §如何记录](./baseline-log.md#如何记录) 锁定，页面只能消费下表字段，不能要求新增字段：

| 字段 | 页面位置 |
|---|---|
| `date` | §C timeline 行首 |
| `release_version` | §Header + §C row |
| `trigger` | §C row "Trigger:" |
| `suite` | §B card title + §C row |
| `dataset_version` | §B card "Dataset" + §C |
| `judge_model_id`, `judge_prompt_version` | §B card "Judge" + §C |
| `prev_score`, `new_score`, `delta` | §B sparkline + §C "Score:" |
| `gate_status` | §A chip + §B card status + §C |
| `langsmith_experiment` | §B/§C 出站链接（拼成 LangSmith URL） |
| `pr_url` | §B/§C 出站链接 |
| `notes` | §C 折叠面板 |
| `new_score.by_category` | §B redteam 卡片的分类条 |
| `new_score.per_sample` | （不展示在主页；点 sample 链接进 LangSmith） |

### 6.2 辅助数据：PRD §9 表（静态）

Gate 阈值表（G1–G6, G10）作为**编译期常量**烤进页面，不动态获取。当 PRD 修订时，需要重新生成。文档必须声明"页面里看到的阈值 = 构建时刻 PRD 的快照"。

### 6.3 不消费的数据

- 实时 LangSmith API（页面不调用）。所有 LangSmith 引用都是**链接**，不是 fetched 数据。
- LangSmith dataset 版本（PRD §7）—— 信任 baseline-log 里的 `dataset_version` 字段。
- 评分内部分布（per-sample 直方图）—— 留给 LangSmith UI，不在本页。

---

## 7. 交互 (Interactions)

页面 **95% 静态**，只有少量克制的交互：

| 元素 | 交互 | 说明 |
|---|---|---|
| §A gate chip | hover → tooltip；click → 滚动锚 | 不弹模态 |
| §B sparkline 点 | hover → 显示该点 `date / score / git_sha` | 不允许 brush / zoom |
| §B redteam category 条 | hover → 显示 `passed/total/rate` | 不可点击下钻 |
| §C notes 折叠 | click → 展开 / 收起 | 仅本卡，不联动 |
| §C `Show all` | click → 渲染剩余 entries | 一次性，不分页 |
| 任何出站链接 | `target="_blank" rel="noopener"` | LangSmith / GitHub |

**禁止**：拖拽、键盘快捷键（除 Tab/Enter 默认）、自动刷新、动效装饰。

---

## 8. Responsive 行为

| 断点 | 行为 |
|---|---|
| ≥ 1280px | §A 7 列；§B 3 列网格 |
| 1024–1279px | §A 7 列；§B 2 列 |
| 768–1023px | §A 2 行 (4+3)；§B 2 列 |
| 480–767px | §A 横向滚动 + scroll-snap；§B 1 列；§C 时间线缩进减半 |
| < 480px | 同上；卡片 metric 字号降一档；sparkline 高度从 80px → 56px |

**永远不允许**：横向滚动整页（仅 §A 在小屏例外）、metric 数字换行、表格折叠成"label: value"伪卡片（baseline timeline 例外，那本来就是垂直布局）。

---

## 9. 可访问性 (A11y)

| 维度 | 要求 |
|---|---|
| 语义 HTML | `<header> <main> <section aria-labelledby> <article>`；timeline 用 `<ol>` |
| 色彩对比 | 所有文本 ≥ WCAG AA (4.5:1)；gate 色与白底对比已在 oklch 计算时锁定 |
| 状态不只靠色 | gate verdict 同时用形状（圆/三角/方）+ 文本标签 |
| 键盘 | 所有链接、折叠按钮 Tab 可达；焦点环 `outline: 2px solid var(--c-ink)` 而不是 `outline:none` |
| 动效 | 全页尊重 `prefers-reduced-motion`；唯一的动效（折叠展开）改成瞬时切换 |
| 图表 | sparkline / category bar 必须有 `<title>` + 表格备份（visually-hidden）供屏读 |

---

## 10. 性能预算 (Performance Budget)

参考全局 `~/.claude/rules/web/performance.md` 的"Microsite"档：

| 资源 | 预算 |
|---|---|
| HTML | < 40 KB gzipped |
| CSS | < 15 KB gzipped |
| JS | **0 KB**（页面不需要运行时；如果一定要做折叠，inline 一段 < 2KB 的 vanilla） |
| 字体 | 最多 2 family × 2 weight；`font-display: swap`；只 preload display serif 的 600 |
| 图片 | 0 张 raster；所有图表都是 inline SVG |
| LCP | < 1.2s（静态托管） |
| CLS | 0（sparkline 占位用 `aspect-ratio`） |

**单页静态文件**：构建产物可以直接 commit 到 `docs/eval/dashboard/index.html`，由 GitHub Pages / 内网 Nginx 出，不需要 Node runtime。

---

## 11. 数据生成与刷新

页面不主动 fetch；由一个离线脚本（**未来任务**，不在本设计文档范围）从 `baseline-log.md` 解析 YAML、拼成 HTML。触发时机与 baseline-log 一致：

- 每次 baseline 重跑写完 baseline-log → 跑生成脚本 → commit 一次。
- 生成脚本必须把 `git rev-parse HEAD` 写进 §D footer，闭环可追溯。

**不接 cron**。手动是 feature，不是 bug——eval baseline 本身不是高频事件。

---

## 12. 验收清单 (Design Acceptance)

设计稿落地前必须满足：

- [ ] 一眼能找到当前 release 是 pass / soft / hard
- [ ] 每个 v0.1 必做 suite（review_martian, swe_bench_lite, redteam_prompt_injection）都有独立卡
- [ ] Deferred suite 显示为"被冻"而不是"缺数据"
- [ ] redteam 卡片可见 6 类 category 拆解
- [ ] 最近 5 条 baseline 可滚动看到，更老的折叠
- [ ] 每条 baseline 能跳到 LangSmith + GitHub PR
- [ ] 印刷友好（reviewer 偶尔打印归档）：A4 黑白可读、链接显示 URL
- [ ] 没有任何"假数据 / Lorem ipsum"残留
- [ ] 页面不依赖外部 CDN / 实时 API
- [ ] 数据 source 与生成时刻在 §D 明示

---

## 13. 未来扩展（明确**不在 v0.1 dashboard**）

记录在此，避免日后被误解为遗漏：

- v0.2：当 PRD §10.3 LLM judge 上线，§B 卡片新增 `judge stability` 副指标。
- v0.2：当 swe_bench / aider 接入，§B grid 自然扩容。
- v0.3：当 internal suite 解冻（PRD §4.0 gate trip），deferred 卡变实时卡。
- 不会做：用户多 tenancy、登录、自定义视图、警报订阅。

---

## 14. 文件归属

- 设计稿（本文件）：`docs/eval/eval-dashboard-design.md`
- 生成脚本（未来）：`evals/scripts/render_dashboard.py`（建议）
- 输出产物（未来）：`docs/eval/dashboard/index.html` + `docs/eval/dashboard/styles.css`
- 数据契约引用：[`baseline-log.md`](./baseline-log.md) · [`openbot-eval-prd.md`](../prd/openbot-eval-prd.md)
