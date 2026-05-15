# OpenBot Eval Runner 开发计划

> 制定日期：2026-05-15  
> 状态：**可执行方案 v1**  
> 相关文档：[PRD](../prd/openbot-prd.md) · [Eval 推荐方案](./eval-setup-recommendation.md) · [Benchmark 调研](./github-bot-evaluation-benchmarks.md) · [Open SWE 现有 eval 解析](./CICD_AND_EVALS_CN.md)

---

## 0. 一句话结论

OpenBot 的 canonical eval stack 锁定为：

| 角色 | 选型 | 责任边界 |
|---|---|---|
| **主 runner** | **Inspect AI** | 组织 dataset / solver / scorer，统一跑 review、triage、bugfix、red-team 评测 |
| **真实 agent 执行后端** | **OpenBot workflow + Modal sandbox** | 跑内部 benchmark 时复用生产路径，保证 shadow / retrospective 信号不失真 |
| **公开 benchmark sandbox** | **Inspect AI 自带 Docker sandbox** | 复用 `inspect_evals` / SWE-bench 生态，不为公开任务重写 harness |
| **观测 / 实验记录** | **LangSmith** | 与 LangChain / LangGraph 原生集成，记录 trace、token、cost、latency、score、版本对比，并承接 dataset / experiment / online eval / annotation queue |
| **调度层** | **GitHub Actions** | 手动触发、cron、异步回归通知；不承担 eval 语义本身 |
| **可选替代** | **Langfuse self-hosted** | 如果后续更重视完全自托管，再替换观测层 |

这意味着：**不要把 GitHub Actions 当 runner，不要把 LangSmith 当统一 runner，也不要因为 Langfuse 可自托管就为了理念纯度增加当前集成成本。**

---

## 1. 为什么现在需要单独做 runner 计划

OpenBot 的 eval 不是一个 homogeneous workload，而是四类完全不同的问题：

| 类型 | 代表任务 | 真值来源 | 对 runner 的要求 |
|---|---|---|---|
| 文本判分型 | Martian review、shadow PR review、triage 分类 | golden set + LLM judge / 标签 | dataset、批量并发、judge、聚合指标 |
| 长链路 agent 型 | SWE-bench Lite / Verified、LIBRO、内部 issue→PR retrospective | 测试是否通过、是否成功复现 | sandbox、repo setup、长耗时、失败恢复 |
| 安全回归型 | prompt injection red-team | fail-safe 断言 | 可重复、便宜、可 gate |
| 线上反馈型 | resolution rate、thumbs up/down、真实 cost | 生产事件 | trace、指标、长期趋势 |

旧的 Open SWE reviewer eval 只解决了第一类中的一个子问题：**给 reviewer agent 做离线黄金集对齐**。它对 `Issue → PR fix`、`reproduce`、`trajectory`、`online resolution rate` 都没有统一抽象。因此它适合作为参考实现，不适合作为 OpenBot 的最终 runner 架构。

---

## 2. 目标与非目标

### 2.1 目标

1. **统一入口**：所有离线 eval 都能通过 `inspect eval ...` 或少量 wrapper command 运行。
2. **统一产物**：每次 run 都产出可比的 score、cost、latency、artifact、trace。
3. **生产一致性**：内部 benchmark 尽量复用真实 OpenBot workflow 与 Modal sandbox，而不是另写一套“只在 eval 里能跑”的 fake path。
4. **分层执行**：把 smoke、regression、weekly、release 四种运行模式拆开，控制成本。
5. **可迁移**：现有 reviewer LangSmith eval 能继续复用数据、experiment 和人工 review 资产，不要求一次性推倒重来。
6. **可扩展**：后续接 GitBugs、Cupid、LIBRO、SWE-bench Pro、online eval 时不用再换 runner。

### 2.2 非目标

1. 不在 v0.1 实现所有 benchmark；v0.1 只做最小闭环。
2. 不追求“所有东西都在 CI 里自动 block merge”；昂贵 eval 不适合做同步 PR gate。
3. 不自研完整 SWE-bench harness；公开 benchmark 优先复用 Inspect / 上游已有实现。
4. 不把 LangSmith 当成统一 agent runner；它负责观测、dataset、experiment、online eval 与人工复核，`Inspect AI` 负责 runner。
5. 不把 Verified 分数当唯一产品质量指标；它只是行业可比信号之一。

---

## 3. 关键架构决策

### 3.1 Runner 与执行后端分离

`Inspect AI` 负责“怎么组织一次评测”，`OpenBot + Modal` 负责“真实 agent 怎么跑”。

这样做的好处：

- 公开 benchmark 可直接吃 `inspect_evals`
- 内部 benchmark 又能复用生产代码路径
- 以后换 sandbox provider 时，不需要重写整套 eval 抽象
- 以后接新的 workflow（Linear、Slack、plugin）时，runner 层不膨胀

### 3.2 双 sandbox 策略

| 场景 | sandbox | 原因 |
|---|---|---|
| `SWE-bench Lite / Verified / Pro` | Inspect Docker sandbox | 与公开 benchmark 生态兼容，最省重写成本 |
| 内部 shadow / retrospective / reproducer | Modal sandbox | 与生产一致，测到的才是 OpenBot 真能力 |
| prompt injection / triage label | 无需重型 sandbox 或最小 fake env | 便宜、可快速跑 |

### 3.3 四级运行模式

| 模式 | 触发 | 目标 | 是否阻塞 |
|---|---|---|---|
| **Smoke** | 本地开发 / PR 手动 | 验证 runner、dataset、solver 没坏 | 不阻塞或只做软提醒 |
| **Regression** | prompt / workflow 相关改动后异步触发 | 防明显退化 | review suite 可 hard gate，重型 suite 不同步 gate |
| **Weekly** | cron | 看趋势 | 不阻塞 merge，触发趋势告警 |
| **Release** | 发版前手动 | 出 baseline / 对外数字 | 可 block release |

这可以同时解决 PRD 里“eval 不进 CI”与“prompt 改动后自动跑回归”的表述冲突：  
**昂贵完整 eval 不进同步 CI；便宜 smoke 和异步 regression 可以由 GitHub Actions 触发。**

### 3.4 指标优先级

| 优先级 | 指标 | 说明 |
|---|---|---|
| P0 | task success / resolved / reproduce success | 终态是否完成 |
| P0 | precision / recall / F1 | review / triage 的核心质量 |
| P1 | cost / task | OpenBot 是 BYO key 产品，成本必须显式可控 |
| P1 | latency / wall time | review 和 triage 直接影响 UX |
| P1 | step count / retries / sandbox restart | 判断 agent 是否绕路 |
| P2 | diff minimality / unrelated change ratio | v0.2 后加入，衡量 PR 质量 |
| P2 | online resolution rate | 上线后作为长期真实信号 |

---

## 4. 目标目录结构

```text
evals/
├── README.md
├── common/
│   ├── config.py                 # suite、model、budget、dataset 路径
│   ├── artifacts.py              # patch、trace、logs、score 导出
│   ├── langsmith.py              # trace / score 上报适配
│   ├── metadata.py               # git sha、prompt version、model、suite version
│   └── judges.py                 # 共享 LLM judge 封装
│
├── solvers/
│   ├── openbot_review.py         # 调 review workflow
│   ├── openbot_triage.py         # 调 triage workflow
│   ├── openbot_fix.py            # 调 issue→PR workflow
│   └── openbot_redteam.py        # 调安全路径
│
├── scorers/
│   ├── review_overlap.py         # precision / recall / f1
│   ├── triage_labels.py          # multi-class / multi-label F1
│   ├── patch_tests.py            # resolved / FAIL_TO_PASS / PASS_TO_PASS
│   ├── reproducer.py             # fail-before-fix / pass-after-fix
│   ├── safety.py                 # fail-safe assertions
│   └── trajectory.py             # step、tool、retry 统计
│
├── datasets/
│   ├── internal_prs_v1.jsonl
│   ├── internal_issues_v1.jsonl
│   ├── prompt_injection_v1.jsonl
│   └── manifests/
│       ├── internal_prs_v1.yaml
│       ├── internal_issues_v1.yaml
│       └── prompt_injection_v1.yaml
│
├── tasks/
│   ├── review_martian.py
│   ├── review_shadow.py
│   ├── triage_internal.py
│   ├── redteam_prompt_injection.py
│   ├── swe_bench_lite.py
│   └── libro_reproducer.py
│
└── scripts/
    ├── build_internal_pr_dataset.py
    ├── build_internal_issue_dataset.py
    ├── export_run_summary.py
    └── compare_runs.py
```

### 4.1 目录设计原则

- `tasks/` 定义“跑什么”
- `solvers/` 定义“OpenBot 怎么执行”
- `scorers/` 定义“怎么算分”
- `datasets/` 存自有数据和版本清单
- `common/` 放跨 suite 的 glue code
- `scripts/` 放一次性构建 / 导出工具

这样以后从 reviewer 扩到 triage、fix、reproducer，不会把所有逻辑塞进一个 `run_eval.py`。

---

## 5. Dataset 设计

### 5.1 通用约束

所有自建 dataset 都必须满足：

1. **版本化**：`internal_prs_v1`、`internal_issues_v1`
2. **冻结输入**：repo / SHA / PR number / expected output 不允许 silent mutation
3. **附带 manifest**：记录来源、采样规则、构建时间、样本数、golden 生成方式
4. **可 spot-check**：每条样本必须能追溯到原始 issue / PR
5. **区分 public 与 private**：不要把内部 repo 的敏感样本混进公开 benchmark

### 5.2 `internal_prs_v1.jsonl`

建议字段：

```json
{
  "id": "repo#1234",
  "repo": "owner/name",
  "pr_number": 1234,
  "base_sha": "abc",
  "head_sha": "def",
  "title": "...",
  "diff_url": "...",
  "golden_comments": [
    {
      "file": "src/foo.py",
      "line": 42,
      "body": "...",
      "severity": "medium",
      "source": "human_review"
    }
  ],
  "metadata": {
    "merged_at": "...",
    "language": "python",
    "repo_segment": "core"
  }
}
```

### 5.3 `internal_issues_v1.jsonl`

建议字段：

```json
{
  "id": "repo#567",
  "repo": "owner/name",
  "issue_number": 567,
  "title": "...",
  "body": "...",
  "labels": ["bug", "priority/P2"],
  "duplicate_of": null,
  "expected_reproducible": true,
  "metadata": {
    "closed_at": "...",
    "language": "typescript"
  }
}
```

### 5.4 `prompt_injection_v1.jsonl`

必须覆盖：

- issue body 注入
- PR comment 注入
- code comment 注入
- fake system prompt 注入
- secret exfiltration 指令
- tool misuse 指令

输出不追求“聪明”，只要求**稳定 fail-safe**。

---

## 6. Suite 设计

### 6.1 Review suite

| Suite | v0.1 | 指标 | 运行模式 |
|---|---|---|---|
| `review_martian` | 必做 | micro / macro precision, recall, F1 | regression + release |
| `review_shadow` | 必做 | precision, recall, F1 + spot-check | smoke + regression + release |

实现要点：

- solver 走真实 review workflow
- scorer 共享 `review_overlap.py`
- judge prompt 与公开 benchmark 尽量保持兼容
- 候选 finding 输出统一归一化成 `{file, line, body, severity}`
- 结果里保留 unmatched golden / unmatched candidate，方便 debug

### 6.2 Triage suite

| Suite | v0.1 | 指标 | 运行模式 |
|---|---|---|---|
| `triage_internal` | 做最小版 | label F1、priority accuracy、reproduce decision accuracy | smoke + regression |
| `gitbugs_subset` | v0.2 | macro / weighted F1 | quarterly |

实现要点：

- v0.1 不急着上 GitBugs，先让自家 issue retrospective 跑通
- label 评估要区分 exact match 与 acceptable match
- priority 要单列，不要和普通 labels 混成一个总 F1

### 6.3 Fix suite

| Suite | v0.1 | 指标 | 运行模式 |
|---|---|---|---|
| `swe_bench_lite` | 必做 | resolved %、cost、wall time | weekly |
| `fix_internal_smoke` | 建议做 5-10 条 | resolved %、artifact 完整性 | smoke |
| `swe_bench_verified` | v0.2 | resolved % | release |
| `swe_bench_pro` | v0.2+ | resolved % | quarterly |

实现要点：

- 公开 benchmark 直接复用 Inspect 生态
- 内部 smoke 只挑 5-10 条，优先验证 OpenBot workflow 能跑通
- score 之外必须保留 patch、test log、失败原因
- 失败分类至少拆成：setup failure / agent failure / test failure / budget stop / timeout

### 6.4 Reproducer suite

| Suite | v0.1 | 指标 | 运行模式 |
|---|---|---|---|
| `reproducer_internal_smoke` | 可选 | fail-before-fix / pass-after-fix | smoke |
| `libro_reproducer` | v0.2 | reproduce success rate | biweekly |

实现要点：

- 如果 v0.1 triage 已经把“尝试复现”作为卖点，至少应有内部 smoke
- 复现成功必须是双条件：修复前失败、修复后通过

### 6.5 Safety suite

| Suite | v0.1 | 指标 | 运行模式 |
|---|---|---|---|
| `redteam_prompt_injection` | 必做 | all fail-safe | smoke + release |

实现要点：

- 必须是 hard gate
- 成本要足够低，允许本地和 CI smoke 都跑
- 失败时输出“被哪类注入击穿”，不是只给总分

---

## 7. 运行与调度策略

### 7.1 本地开发

```bash
# 3 条 review shadow，验证 runner / solver / judge 正常
inspect eval evals/tasks/review_shadow.py --limit 3

# 5 条安全红队，验证 fail-safe 没坏
inspect eval evals/tasks/redteam_prompt_injection.py --limit 5
```

### 7.2 PR 级异步回归

触发条件：

- `openbot/**`
- `prompts/**`
- `evals/**`
- `.openbot/config.yaml` 中影响 agent 行为的字段

执行内容：

| 变更类型 | 自动跑 |
|---|---|
| review prompt / review workflow | `review_shadow` 小样本 + 可选 `review_martian` |
| triage prompt / triage workflow | `triage_internal` 小样本 |
| safety middleware / prompt wrapper | `redteam_prompt_injection` 全量 |
| fix workflow / sandbox | `fix_internal_smoke` |

策略：

- PR 上先发评论，不默认同步阻塞
- 只有 `review_martian` 退化超过 hard threshold、或 `redteam` 失败时才 block merge
- 昂贵 suite 放到异步 job，不跟 `ruff/pytest` 串行

### 7.3 周期性任务

| 频率 | Suite |
|---|---|
| 每周 | `swe_bench_lite`、`review_shadow` 全量、trajectory summary |
| 每两周 | `libro_reproducer`（v0.2） |
| 每月 / release | `review_martian`、`swe_bench_verified`、完整 cost report |
| 每季度 | `swe_bench_pro`、`gitbugs_subset` |

### 7.4 Release gate

发版前必须满足：

1. `redteam_prompt_injection` 全绿
2. `review_shadow` 不低于 baseline
3. `review_martian` 未触发 hard regression
4. `swe_bench_lite` 无连续退化趋势
5. 本次 release report 已归档到 `docs/reports/` 或外部 dashboard

---

## 8. LangSmith 设计

### 8.1 每次 run 必须写入的 metadata

```text
suite_name
suite_version
dataset_version
git_sha
prompt_version
workflow_version
model_id
judge_model_id
sandbox_backend
runner_version
started_at
```

### 8.2 每条 sample 至少记录

```text
sample_id
input artifact refs
output artifact refs
score payload
tokens_in / tokens_out
cost_usd
latency_ms
step_count
tool_call_count
retry_count
sandbox_restart_count
failure_category
```

### 8.3 Experiment 命名

推荐：

```text
{suite}-{dataset_version}-{git_sha_short}-{model_alias}
```

例：

```text
review-shadow-v1-a1b2c3-sonnet45
swe-lite-upstream-2026w20-haiku45
```

---

## 9. 迁移方案：从 reviewer-only LangSmith runner 到 `Inspect + LangSmith`

### 9.1 保留什么

- golden comments 数据格式
- Martian judge 思路
- micro / macro 聚合方式
- target / scorer 分离
- LangSmith dataset / experiment / annotation 资产

### 9.2 放弃什么

- 单个 `run_eval.py` 包揽所有事情
- 进程内全局 dict 传 aggregate state
- reviewer-only 的目录组织
- 把 LangSmith 当作所有 agent eval 的执行框架

### 9.3 迁移顺序

1. 先把现有 reviewer dataset 接到 `review_martian` 的 Inspect task
2. 用相同 judge prompt 跑一轮，对齐旧 LangSmith runner 的分数
3. 确认差异只来自 runner，而非 scoring logic
4. 再接 `review_shadow`
5. 保留 LangSmith 作为 trace / experiment / online eval 平台，不再让它承担统一 runner 职责

### 9.4 迁移完成判据

- Inspect 版 `review_martian` 与旧 LangSmith runner 版 score 差异在可解释范围内
- 相同 dataset、相同 model、相同 judge 下，可重复跑出稳定结果
- 旧路径不再承载新 benchmark

---

## 10. 分阶段开发计划

## Phase 0 · 设计冻结（0.5 周）

**目标**：先把最容易返工的边界锁死。

产出：

- `evals/` 目录 skeleton
- suite 命名规则
- dataset manifest schema
- LangSmith metadata schema
- smoke / regression / weekly / release 四级运行策略

完成判据：

- 任意一个新 benchmark 都能说清楚“放哪、怎么跑、怎么算、何时跑”

## Phase 1 · 最小闭环（第 1 周）

**目标**：先让 `Inspect + OpenBot solver + LangSmith` 真跑起来。

任务：

1. 接入依赖：`inspect-ai`、`inspect-evals`
2. 建 `evals/common/`、`evals/solvers/`、`evals/scorers/` skeleton
3. 写 `review_shadow` 最小 task
4. 做 3-5 条 `internal_prs_v1` smoke dataset
5. 打通 LangSmith tracing + score 上报
6. 输出首份 run summary

验收：

- 本地一条命令能跑 3 个样本
- 每个样本有 trace、score、cost、artifact
- 失败时能从 artifact 找到原因

## Phase 2 · v0.1 必需 suite（第 2-3 周）

**目标**：覆盖 v0.1 发布前真正需要看的信号。

任务：

1. `review_martian`
2. `review_shadow` 全量
3. `redteam_prompt_injection`
4. `triage_internal` 最小版
5. `fix_internal_smoke`
6. `swe_bench_lite` 上游 task 接入
7. `compare_runs.py` + threshold 配置

验收：

- PR prompt 变更后能自动跑 review regression
- release 前能一键产出 `review + safety + fix smoke` 报告
- `swe_bench_lite` 周跑能落地并产出趋势图原始数据

## Phase 3 · 调度与报告（第 4 周）

**目标**：让 eval 从“能跑”变成“稳定运转”。

任务：

1. `workflow_dispatch`
2. weekly cron
3. release manual workflow
4. PR comment summary
5. run summary 导出
6. baseline compare
7. cost dashboard 字段校验

验收：

- 周跑无需手工介入
- baseline 对比自动生成
- regression 结果能回到 PR 或 release note

## Phase 4 · v0.2 扩展（后续）

任务：

1. `gitbugs_subset`
2. `libro_reproducer`
3. `swe_bench_verified`
4. `swe_bench_pro`
5. trajectory scorer
6. online resolution rate ingestion
7. diff minimality / unrelated change scorer

---

## 11. Backlog 拆分建议

### Epic A · Eval foundation

- 建立 `evals/` 目录骨架
- 加 Inspect / LangSmith 依赖
- 建 metadata / artifact / config 公共层
- 统一 run summary schema

### Epic B · Dataset pipeline

- 构建 `internal_prs_v1`
- 构建 `internal_issues_v1`
- 构建 `prompt_injection_v1`
- 加 manifest 与 dataset validator

### Epic C · Review eval

- review solver
- Martian scorer
- shadow scorer
- unmatched debug artifact
- baseline compare

### Epic D · Safety eval

- red-team dataset
- fail-safe scorer
- hard gate

### Epic E · Fix eval

- fix smoke solver
- SWE-bench Lite integration
- patch artifact exporter
- failure classification

### Epic F · Automation

- PR regression workflow
- weekly workflow
- release workflow
- PR comment reporter

### Epic G · v0.2 expansion

- triage public benchmark
- reproducer benchmark
- trajectory scorer
- online feedback ingestion

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 内部 eval 不复用真实 workflow | 分数漂亮但不代表线上能力 | 内部 suite 强制走真实 OpenBot workflow + Modal |
| 所有 suite 都想进 PR gate | 成本爆炸、PR 周期变慢 | 只让安全红队和少量 review regression hard gate |
| judge 漂移 | 指标不可比 | judge model / prompt 版本化，release report 固定版本 |
| dataset 静默变更 | baseline 失效 | dataset manifest + immutable version |
| 只盯终态，不看轨迹 | agent 变慢、变贵但总分不掉 | step / retry / restart 作为 P1 指标 |
| Verified 过拟合 | 对外分高，真实收益低 | shadow / Pro / online resolution 并列看 |
| Modal 与 Inspect Docker 行为差异 | 公开 benchmark 与真实运行不一致 | 明确双 sandbox 策略，不混淆结论 |
| 过早上太多 benchmark | 维护成本大于收益 | v0.1 只做 review、safety、lite、内部 smoke |

---

## 13. v0.1 完成定义

满足以下条件，才算 eval runner v0.1 完成：

1. `Inspect AI` 已成为唯一推荐的新离线 runner
2. `review_shadow`、`review_martian`、`redteam_prompt_injection`、`fix_internal_smoke` 已可运行
3. `swe_bench_lite` 已接入周跑
4. 每次 run 都能在 LangSmith 找到 trace、score、cost、artifact
5. prompt / review workflow 变更后能自动触发异步 regression
6. release 前有一条明确命令能产出汇总报告
7. 文档中不再混用“CI gate”“异步 regression”“release gate”三个概念

---

## 14. 建议立即执行的下一步

按优先级排序：

1. 冻结 `evals/` 目录结构和 metadata schema
2. 先做 `review_shadow` 3 条 smoke，不要一上来接最重的 benchmark
3. 让 `OpenBot review workflow` 先被一个 Inspect solver 调起来
4. 把 LangSmith trace / score / cost 打通
5. 再迁移 Martian review
6. 最后再接 `SWE-bench Lite`

这条路线的理由很简单：**先证明“你的 runner 能跑你自己的 agent”，再去跑别人已经准备好的 benchmark。**

---

## 15. 后续文档建议

这份计划落地后，建议补三份更短、更偏执行的文档：

1. `evals/README.md`：给开发者看的日常运行说明
2. `docs/research/eval-dataset-spec.md`：内部 dataset 字段规范
3. `docs/research/eval-gating-policy.md`：smoke / regression / weekly / release 的阈值与行为

它们不应该再重复解释“为什么选 Inspect AI”，而是只回答“怎么跑、怎么加样本、什么时候 fail”。
