# OpenBot Eval · Task List

> 起草日期：2026-05-15 · 状态：可执行
> 上位文档：[`openbot-eval-prd.md`](../prd/openbot-eval-prd.md) · [`eval-runner-development-plan.md`](../research/eval-runner-development-plan.md)
> 用途：把 eval PRD §14 milestones 拆成 PR 颗粒度任务，可直接 `gh issue create` 批量导入

---

## 范围调整 · 2026-05-15

**Internal-data-dependent 的全部 suite DEFERRED。** 详见 [`openbot-eval-prd.md §4.0`](../prd/openbot-eval-prd.md#40-范围调整--internal-data-dependent-suite-全部-deferred2026-05-15-锁定)。

理由：项目处在 v0.1 Week 1 skeleton 阶段，自家 repo 真实数据量不足以建可信 dataset。提前构建等于自造样本，跑出来的分数没意义。等 dogfood 期数据积累到 §4.0 表的 gate 后再启动。

**v0.1 / v0.2 期间不做的任务（带 🕒 标记）**

| 原任务 ID | 标题 | 解冻条件（PRD §4.0） |
|---|---|---|
| 🕒 E2-T03 | `internal_issues_v1` dataset | ≥ 50 closed issue 含 label/priority/dup-of |
| 🕒 E2-T04 | Triage solver | 同上 |
| 🕒 E2-T05 | Triage labels scorer | 同上 |
| 🕒 E2-T06 | `triage_internal` task | 同上 |
| 🕒 E2-T09 | `fix_internal_smoke` dataset | ≥ 10 issue→PR retrospective |
| 🕒 E2-T10 | `fix_internal_smoke` task | 同上 |
| 🕒 E4-T01 | `internal_prs_v1` dataset | ≥ 30 PR 含 ≥ 2 reviewer comments |
| 🕒 E4-T08 | `internal_issues_v2` 扩容 | triage gate trip 之后 |
| 🕒 E5-T01 | `internal_prs_v2` 扩到 100 PR | review gate trip 之后 |
| 🕒 E5-T02 | `review_shadow` task | review gate trip |
| 🕒 E5-T03 | `review_shadow_xl` task | review gate trip |

**保留的任务**（基础设施 + 公开 benchmark + safety）—— 共 **49** 条：

| 留下做 | 性质 |
|---|---|
| E0-T01..T05 | 设计冻结 + 治理模板 |
| E1-T01..T10 | runner / langsmith / solver / scorer / dataset 基础设施 |
| E2-T01/T02 | `review_martian` baseline（公开） |
| E2-T07/T08 | fix solver + patch_tests scorer（被 `swe_bench_lite` 复用） |
| E2-T11 | `swe_bench_lite`（公开） |
| E2-T12..T14 | `redteam_prompt_injection`（手写，不依赖真实数据） |
| E2-T15/T16 | `compare_runs.py` + failure category 校验 |
| E3 全部 9 条 | 调度与报告 |
| E4-T02..T07, T09, T10 | 公开 benchmark 扩张 + red-team 扩容 + online resolution rate |
| E5-T04..T06 | `swe_bench_pro` + diff minimality + annotation queue |
| CR-T01..T04 | 持续治理 |

**新增任务**

### CR-T05 · Data accumulation tracker（v0.2 起执行）
- **Stream**: GOV
- **Goal**: 上线后开始累计 §4.0 表中 4 个 gate 的真实计数
- **Deliverables**: `docs/eval/data-accumulation.md`，月度回写：PR 累计数 / issue 累计数 / retrospective 累计数 / reproducer 累计数
- **AC**: 任一 gate trip 时 PRD §17 #15 触发解冻流程，开新 milestone E-Internal sub-task
- **Deps**: main PRD v0.2 上线
- **Effort**: S（持续，每月 ~30min）
- **PRD ref**: §4.0 · §17 #15

---

## 编号规则

`E{milestone}-T{NN}` —— 例：`E1-T03` = Milestone E1 的第 3 个任务。

每条任务包含：
- **Goal** —— 一句话目标
- **Deliverables** —— 必须产出的文件 / 行为
- **AC** —— Acceptance Criteria（验收点）
- **Deps** —— 依赖的前置任务
- **Effort** —— S (< 0.5 day) · M (0.5–1.5 day) · L (1.5–3 day)
- **PRD ref** —— 锚定到 PRD 哪一段

---

## Stream 划分

跨 milestone 的 7 条工作流：

| Stream | 缩写 | 内容 |
|---|---|---|
| Infrastructure | INF | 目录骨架、依赖、配置、CI 接线 |
| Common library | LIB | `evals/common/*` 共享代码 |
| Solvers | SOL | `evals/solvers/*` 调真实 OpenBot workflow |
| Scorers | SCR | `evals/scorers/*` 算分逻辑 |
| Datasets | DAT | `evals/datasets/*` + manifest |
| Suites | SUI | `evals/tasks/*` 一个 suite 一个文件 |
| Observability | OBS | LangSmith 接线 + 校验脚本 |
| Scheduler | SCH | GitHub Actions workflow |
| Governance | GOV | baseline-log / judge-log / cost-report |

---

## Milestone E0 · 设计冻结（本周）

### E0-T01 · 合并 eval PRD
- **Stream**: GOV
- **Goal**: 把 `docs/prd/openbot-eval-prd.md` 合入 main 并在 main PRD §8 / §15 References 加交叉引用
- **Deliverables**: PR 合并；main PRD §8 标题下方加 "完整 spec 见 eval PRD"
- **AC**: `docs/prd/openbot-prd.md` §8 第一句指向 eval PRD
- **Deps**: —
- **Effort**: S
- **PRD ref**: §1

### E0-T02 · 建 `evals/` 目录骨架
- **Stream**: INF
- **Goal**: 按 PRD §6.1 生成空目录与占位 `__init__.py`
- **Deliverables**:
  ```
  evals/{common,solvers,scorers,datasets/manifests,tasks,scripts}/__init__.py
  evals/README.md  (1-page index of suites + how to run)
  ```
- **AC**: `tree evals/` 与 PRD §6.1 一致；`evals/README.md` 列出全部 v0.1 suite 名
- **Deps**: E0-T01
- **Effort**: S
- **PRD ref**: §6.1

### E0-T03 · 治理日志模板
- **Stream**: GOV
- **Goal**: 建 baseline / judge / flaky 三本日志的空模板
- **Deliverables**:
  ```
  docs/eval/baseline-log.md
  docs/eval/judge-version-log.md
  docs/eval/flaky-samples.md
  docs/eval/dataset-spot-check/.gitkeep
  ```
- **AC**: 三本日志含 "如何记录" 一节 + 第一行 placeholder
- **Deps**: E0-T01
- **Effort**: S
- **PRD ref**: §9 baseline 治理 · §10.3 · §12.2

### E0-T04 · LangSmith run 校验脚本占位
- **Stream**: OBS
- **Goal**: 落 `scripts/validate_langsmith_run.py` 占位（PRD §10.1 / §10.4 校验逻辑后续填）
- **Deliverables**: 脚本 + 含 `--help`；CI 中保留 placeholder job（noop）
- **AC**: `python scripts/validate_langsmith_run.py --help` 列出 metadata / experiment naming 校验项
- **Deps**: E0-T02
- **Effort**: S
- **PRD ref**: §10.1 §10.4

### E0-T05 · Budget 常量与 config 模块
- **Stream**: LIB
- **Goal**: 建 `evals/common/config.py`，固化 PRD §11.1 三层 budget 数值
- **Deliverables**: 含 `PER_SAMPLE_USD`、`PER_SUITE_RUN_USD`、`MONTHLY_TOTAL_USD` 常量 + 单测
- **AC**: `pytest tests/eval/test_config.py` 跑通；常量值与 PRD §11.1 一字不差
- **Deps**: E0-T02
- **Effort**: S
- **PRD ref**: §11.1

---

## Milestone E1 · 最小闭环（v0.1 Week 1-2）

> **目标**：本地一条命令跑 3-5 个 sample，trace / score / cost 都在 LangSmith。

### E1-T01 · 加 `inspect-ai` + `inspect-evals` 依赖
- **Stream**: INF
- **Goal**: `uv add inspect-ai inspect-evals`
- **Deliverables**: `pyproject.toml` 更新；`uv sync` 通过；CI 装包成功
- **AC**: `uv run inspect --version` 输出版本号
- **Deps**: E0-T02
- **Effort**: S
- **PRD ref**: §17 #1

### E1-T02 · LangSmith env 与 client 封装
- **Stream**: LIB
- **Goal**: 建 `evals/common/langsmith.py`，封装 `init_client()` / `log_run_metadata()` / `log_sample()`
- **Deliverables**: 模块 + 单测（mock client）；`.env.example` 加 `LANGSMITH_API_KEY`、`LANGSMITH_PROJECT_INTERNAL`、`LANGSMITH_PROJECT_PUBLIC`
- **AC**: 单测覆盖 §10.1 全部 12 个必填字段；缺字段时 `raise ValueError`
- **Deps**: E1-T01, E0-T05
- **Effort**: M
- **PRD ref**: §10.1 · §13.2

### E1-T03 · Metadata 收集模块
- **Stream**: LIB
- **Goal**: 建 `evals/common/metadata.py` 收集 git_sha / prompt_version / workflow_version 等 run-level metadata
- **Deliverables**: `collect_run_metadata(suite_name, mode) -> dict`
- **AC**: 任意 cwd 调一次能产出完整 dict；单测 mock subprocess git 调用
- **Deps**: E1-T02
- **Effort**: S
- **PRD ref**: §10.1

### E1-T04 · Artifacts 导出模块
- **Stream**: LIB
- **Goal**: 建 `evals/common/artifacts.py` 把 patch / log / trace 上传到 LangSmith artifact store
- **Deliverables**: `export_artifact(sample_id, kind, content)`
- **AC**: 与 LangSmith API 集成测试通过（minimum 1 sample）
- **Deps**: E1-T02
- **Effort**: M
- **PRD ref**: §10.2

### E1-T05 · Judge 封装
- **Stream**: LIB
- **Goal**: 建 `evals/common/judges.py`，固定 `claude-opus-4-7` 为 default judge，prompt 走版本化字符串常量
- **Deliverables**: `Judge` class + `JUDGE_PROMPT_VERSION` 常量；改 prompt 必须 bump 版本号
- **AC**: 单测：注入 judge prompt 改动后 `version` 必 bump，否则 fail
- **Deps**: E1-T01
- **Effort**: M
- **PRD ref**: §10.3 · §17 #5

### E1-T06 · Review solver（真实 workflow 入口）
- **Stream**: SOL
- **Goal**: 建 `evals/solvers/openbot_review.py`，调真实 `openbot.workflows.review.run(...)`
- **Deliverables**: `@solver def openbot_review_solver()` —— 把 PR diff 喂给真实 review workflow，返回结构化 findings
- **AC**: 集成测试用 fake PR diff，确认调到真实 workflow（不 mock）；输出归一化为 `{file, line, body, severity}`
- **Deps**: E1-T04, E1-T05
- **Effort**: L
- **PRD ref**: §4.1 强制 · §6.2

### E1-T07 · Review overlap scorer
- **Stream**: SCR
- **Goal**: 建 `evals/scorers/review_overlap.py`，按 LLM judge 算 precision / recall / F1
- **Deliverables**: `@scorer def review_overlap_scorer()`；保留 unmatched golden / unmatched candidate
- **AC**: 单测覆盖三种 case：完全 match / 部分 match / 完全 miss；judge 调用走 §10.3 锁定 model
- **Deps**: E1-T05
- **Effort**: M
- **PRD ref**: §4.1 · §6.1

### E1-T08 · Martian 最小 task（3-5 sample 验通）
- **Stream**: SUI
- **Goal**: 建 `evals/tasks/review_martian.py`，先用 Martian 上游 5 条样本跑通端到端
- **Deliverables**: Inspect task 文件 + `inspect eval evals/tasks/review_martian.py --limit 5` 跑成功
- **AC**: 跑完每 sample 有 trace / score / cost / artifact；LangSmith run metadata 校验脚本通过
- **Deps**: E1-T06, E1-T07, E0-T04
- **Effort**: M
- **PRD ref**: §4.1 · §14 E1 验收

### E1-T09 · Run summary 导出脚本
- **Stream**: OBS
- **Goal**: 建 `evals/scripts/export_run_summary.py`，从 LangSmith 拉一次 run 的聚合指标输出 markdown
- **Deliverables**: 脚本 + 示例输出 `docs/reports/eval-sample-summary.md`
- **AC**: 输出含：suite 名 / 总 sample / 通过率 / 总 cost / failure_category 分布 / 异常 top-5
- **Deps**: E1-T08
- **Effort**: M
- **PRD ref**: §11.3 · §14 E1 验收

### E1-T10 · 双 LangSmith project 隔离
- **Stream**: OBS · GOV
- **Goal**: 在 LangSmith 建 `openbot-eval-internal` / `openbot-eval-public` 两个项目，按 dataset.public 字段路由
- **Deliverables**: 路由逻辑写入 `evals/common/langsmith.py`；`.env.example` 更新
- **AC**: 一条内部 sample 写入 internal project 而非 public（手动验证 + 单测）
- **Deps**: E1-T02
- **Effort**: S
- **PRD ref**: §13.2

---

## Milestone E2 · v0.1 必需 suite（v0.1 alpha 出货前）

> **目标**：PR 改 review/fix/triage prompt 后自动跑 regression；release 一键出 review + safety + fix smoke 报告。

### E2-T01 · Martian dataset 锁版本
- **Stream**: DAT
- **Goal**: 锁 Martian benchmark 的 upstream commit hash 到 `evals/datasets/manifests/martian_2026w20.yaml`
- **Deliverables**: manifest 含 commit SHA + 引入日期 + sample 数
- **AC**: 任意人 `inspect eval review_martian` 都能拉到同一份 50 PR
- **Deps**: E1-T08
- **Effort**: S
- **PRD ref**: §7.2

### E2-T02 · Martian 全量 run + baseline
- **Stream**: SUI · GOV
- **Goal**: 跑 Martian 50 PR 全量，记录 baseline 到 `docs/eval/baseline-log.md`
- **Deliverables**: 第一条 baseline 条目（含 git_sha / model / judge / F1 / cost）
- **AC**: baseline F1 ≥ 0.35（main PRD §11.1）；否则修 prompt 重跑
- **Deps**: E2-T01
- **Effort**: M
- **PRD ref**: §9 baseline 治理 · §15.2

### 🕒 E2-T03 · `internal_issues_v1` dataset · DEFERRED（解冻：≥ 50 closed issue 含 label）
- **Stream**: DAT
- **Goal**: 从自家 repo 抽 50-100 个 closed issue，含 label / dup-of / reproduce 决策
- **Deliverables**:
  ```
  evals/datasets/internal_issues_v1.jsonl
  evals/datasets/manifests/internal_issues_v1.yaml
  evals/scripts/build_internal_issue_dataset.py
  docs/eval/dataset-spot-check/internal_issues_v1.md
  ```
- **AC**: manifest 含 SHA256 + PII redact 说明；spot-check 文档 ≥ 5 条样本回溯到 issue URL
- **Deps**: E0-T02
- **Effort**: L
- **PRD ref**: §7

### 🕒 E2-T04 · Triage solver · DEFERRED
- **Stream**: SOL
- **Goal**: 建 `evals/solvers/openbot_triage.py`，调真实 triage workflow
- **Deliverables**: solver 文件 + 集成测试
- **AC**: 不 mock workflow；输出含 labels + priority + reproduce_decision
- **Deps**: E1-T06
- **Effort**: M
- **PRD ref**: §4.2 强制

### 🕒 E2-T05 · Triage labels scorer · DEFERRED
- **Stream**: SCR
- **Goal**: 建 `evals/scorers/triage_labels.py`，label F1 + priority accuracy + reproduce decision accuracy **分开**报
- **Deliverables**: scorer + alias map（exact vs acceptable）
- **AC**: priority **不**混入 label F1；单测三个指标独立可读
- **Deps**: E2-T04
- **Effort**: M
- **PRD ref**: §4.2 强制

### 🕒 E2-T06 · `triage_internal` task · DEFERRED
- **Stream**: SUI
- **Goal**: 建 `evals/tasks/triage_internal.py` 接入 `internal_issues_v1`
- **Deliverables**: Inspect task + smoke `--limit 20`
- **AC**: G5 gate（label F1 ≥ 0.55）触发逻辑就位
- **Deps**: E2-T03, E2-T05
- **Effort**: M
- **PRD ref**: §4.2 · §9 G5

### E2-T07 · Fix solver + Modal sandbox 集成
- **Stream**: SOL
- **Goal**: 建 `evals/solvers/openbot_fix.py`，调真实 fix workflow + 真 Modal sandbox
- **Deliverables**: solver；artifact 上报 patch + test log
- **AC**: 不 mock sandbox；至少 1 条 sample 跑出绿测
- **Deps**: E1-T06
- **Effort**: L
- **PRD ref**: §4.3 · §6.2

### E2-T08 · Patch tests scorer
- **Stream**: SCR
- **Goal**: 建 `evals/scorers/patch_tests.py` 算 resolved / FAIL_TO_PASS / PASS_TO_PASS
- **Deliverables**: scorer + 单测
- **AC**: 与 SWE-bench 上游 grading 对齐（同一 sample 同分）
- **Deps**: E2-T07
- **Effort**: M
- **PRD ref**: §4.3 强制

### 🕒 E2-T09 · `fix_internal_smoke` dataset（5-10 条）· DEFERRED（解冻：≥ 10 issue→PR retrospective）
- **Stream**: DAT
- **Goal**: 从自家 repo 抽 5-10 条 issue→PR retrospective
- **Deliverables**: jsonl + manifest + build script
- **AC**: manifest 含 SHA256；样本含 base_sha / fail_test_cmd / pass_test_cmd
- **Deps**: E0-T02
- **Effort**: M
- **PRD ref**: §4.3

### 🕒 E2-T10 · `fix_internal_smoke` task · DEFERRED
- **Stream**: SUI
- **Goal**: 建 `evals/tasks/fix_internal_smoke.py`
- **Deliverables**: Inspect task
- **AC**: smoke 模式 ≤ 2 min；单 sample budget ≤ $3
- **Deps**: E2-T07, E2-T08, E2-T09
- **Effort**: M
- **PRD ref**: §4.3 · §11.1

### E2-T11 · `swe_bench_lite` 接入
- **Stream**: SUI
- **Goal**: 把 OpenBot fix solver 包成可喂给 `inspect_evals/swe_bench_lite` 的形态
- **Deliverables**: `evals/tasks/swe_bench_lite.py` wrapper；docker sandbox 配置
- **AC**: `inspect eval inspect_evals/swe_bench_lite --solver evals.solvers.openbot_fix` 跑 5 条样本通过
- **Deps**: E2-T07
- **Effort**: L
- **PRD ref**: §4.3 · §6 双 sandbox

### E2-T12 · `prompt_injection_v1` dataset
- **Stream**: DAT
- **Goal**: 手写 ≥ 20 条 prompt injection case，覆盖 PRD §13.1 全部 6 类
- **Deliverables**:
  ```
  evals/datasets/prompt_injection_v1.jsonl
  evals/datasets/manifests/prompt_injection_v1.yaml
  docs/eval/dataset-spot-check/prompt_injection_v1.md  (列出每类至少 3 条)
  ```
- **AC**: 6 类覆盖矩阵在 spot-check 文档中显式列出；hash 入 manifest
- **Deps**: E0-T02
- **Effort**: M
- **PRD ref**: §13.1 · §4.5

### E2-T13 · Safety scorer
- **Stream**: SCR
- **Goal**: 建 `evals/scorers/safety.py` —— fail-safe 断言，任意 leak / 调禁用 tool / 接受恶意指令 = fail
- **Deliverables**: scorer + 单测覆盖三种 fail 路径
- **AC**: 输出 "被哪类注入击穿"，不是只给总分
- **Deps**: E2-T12
- **Effort**: M
- **PRD ref**: §4.5 强制 · §13.1

### E2-T14 · `redteam_prompt_injection` task（hard gate）
- **Stream**: SUI
- **Goal**: 建 `evals/tasks/redteam_prompt_injection.py`
- **Deliverables**: Inspect task；cost ≤ $1/次
- **AC**: 跑出 100% fail-safe；任何一条 leak 即整个 task fail；CI 中作为 G6 hard gate
- **Deps**: E2-T12, E2-T13
- **Effort**: M
- **PRD ref**: §4.5 · §9 G6

### E2-T15 · `compare_runs.py` + threshold 配置
- **Stream**: OBS
- **Goal**: 建 `evals/scripts/compare_runs.py` 把当前 run 与 baseline 比对，按 §9 G1/G2/G5 输出 soft/hard
- **Deliverables**: 脚本 + `evals/common/thresholds.py`（数值与 PRD §9 一字不差）
- **AC**: 喂入两次 run，能输出 PR comment markdown（含 ↓X% / status）
- **Deps**: E2-T02
- **Effort**: M
- **PRD ref**: §9

### E2-T16 · Failure category 枚举校验
- **Stream**: OBS
- **Goal**: 实现 §12.4 固定枚举校验，禁止自由文本
- **Deliverables**: 在 `validate_langsmith_run.py` 中加入校验逻辑 + 单测
- **AC**: 写入非枚举值 raise；CI 校验阻止 PR
- **Deps**: E0-T04
- **Effort**: S
- **PRD ref**: §12.4

---

## Milestone E3 · 调度与报告（v0.1 → v0.2 过渡）

### E3-T01 · `eval-regression.yml` workflow
- **Stream**: SCH
- **Goal**: 实现 PR path matcher → 异步触发对应 regression suite
- **Deliverables**: `.github/workflows/eval-regression.yml`
- **AC**: PR 改 `openbot/prompts/review*` 自动跑 `review_martian` (limit 10) + `redteam_prompt_injection`；其他 matcher 同 §8.2 表
- **Deps**: E2-T15
- **Effort**: M
- **PRD ref**: §8.2

### E3-T02 · `eval-weekly.yml` workflow
- **Stream**: SCH
- **Goal**: 周一 02:00 UTC cron 跑 `swe_bench_lite` 全量 + `review_martian` 全量
- **Deliverables**: workflow 文件 + cost guardrail（超 §11.1 weekly budget abort）
- **AC**: 周跑无需手工介入；超 budget 自动 abort 并 alert
- **Deps**: E2-T11, E2-T02
- **Effort**: M
- **PRD ref**: §8.3 · §11.2

### E3-T03 · `eval-release.yml` workflow
- **Stream**: SCH
- **Goal**: `workflow_dispatch` 触发 release suite + 自动生成 release report
- **Deliverables**: workflow 文件；输入 `version`；产出 `docs/reports/eval-release-<version>.md`
- **AC**: 跑完后 PR 自动 commit release report；G3/G6 fail 时 workflow fail
- **Deps**: E3-T02
- **Effort**: M
- **PRD ref**: §8.3 · §14 E3

### E3-T04 · PR comment summary action
- **Stream**: SCH · OBS
- **Goal**: regression 跑完后把 §9 gate 结果贴回 PR 评论
- **Deliverables**: composite action `.github/actions/eval-pr-comment/`
- **AC**: comment 含 suite 名 / 当前分 / baseline 分 / Δ / status (soft/hard/pass)
- **Deps**: E3-T01, E2-T15
- **Effort**: M
- **PRD ref**: §9 · §14 E3 验收

### E3-T05 · Cost report 导出
- **Stream**: GOV · OBS
- **Goal**: release run 后导出 `docs/reports/eval-cost-<version>.md`
- **Deliverables**: 脚本 + 模板；含 suite cost / per-sample / top-10 异常
- **AC**: 与上次 release 的 cost diff 自动计算
- **Deps**: E3-T03
- **Effort**: M
- **PRD ref**: §11.3

### E3-T06 · Flaky sample 自动标记
- **Stream**: GOV · OBS
- **Goal**: 跑完后扫描最近 3 次 run，标记 transient ≥ 2 次的 sample 为 `flaky`
- **Deliverables**: `evals/scripts/flag_flaky_samples.py` + cron weekly；自动更新 `docs/eval/flaky-samples.md`
- **AC**: flaky sample 从聚合分剔除（但 raw record 保留）
- **Deps**: E2-T16
- **Effort**: M
- **PRD ref**: §12.2

### E3-T07 · Suite-level abort（transient > 20%）
- **Stream**: LIB
- **Goal**: 在 runner wrapper 中实现 "transient 失败 > 20% 即 abort 整 run"
- **Deliverables**: `evals/common/abort.py` + 集成测试
- **AC**: 单测：注入 25% transient sample，run abort 不出聚合分
- **Deps**: E2-T16
- **Effort**: S
- **PRD ref**: §12.3

### E3-T08 · Monthly total budget 强制
- **Stream**: GOV · LIB
- **Goal**: 实现 `openbot eval budget` CLI 与 GHA 集成（超 $1500/月 暂停所有 workflow_dispatch）
- **Deliverables**: CLI 子命令 + GHA pre-check step
- **AC**: 手动 `openbot eval budget reset` 恢复；audit log 记录
- **Deps**: E0-T05
- **Effort**: M
- **PRD ref**: §11.1 §11.2

### E3-T09 · Experiment 命名校验上线
- **Stream**: OBS
- **Goal**: 把 §10.4 命名规则校验加进 CI 阻塞流
- **Deliverables**: `validate_langsmith_run.py` 增强 + CI step
- **AC**: 命名违反 → CI 红
- **Deps**: E0-T04, E3-T01
- **Effort**: S
- **PRD ref**: §10.4

---

## Milestone E4 · v0.2 扩展

### 🕒 E4-T01 · `internal_prs_v1` dataset（30-50 PR）· DEFERRED（解冻：≥ 30 PR 含 ≥ 2 reviewer comments）
- **Stream**: DAT
- **Goal**: 抽自家 repo merged PR，含 golden comments + PII redact
- **Deliverables**: jsonl + manifest + build script + spot-check 文档
- **AC**: 60% python / 25% ts / 15% other 分层；每条样本能回溯到 PR URL
- **Deps**: E0-T02
- **Effort**: L
- **PRD ref**: §4.1 · §7

### E4-T02 · `gitbugs_subset` task
- **Stream**: SUI · DAT
- **Goal**: 接入 GitBugs test split（500-1000 sample），quarterly 跑
- **Deliverables**: task + dataset manifest（upstream commit hash）
- **AC**: macro / weighted F1 双指标输出；不进 PR gate
- **Deps**: E2-T05
- **Effort**: M
- **PRD ref**: §4.2

### E4-T03 · `libro_reproducer` task
- **Stream**: SUI · SCR · DAT
- **Goal**: 接入 LIBRO + BugsInPy，双条件 scorer（fail-before-fix ∧ pass-after-fix）
- **Deliverables**: task + `evals/scorers/reproducer.py` + biweekly cron
- **AC**: 至少 1 条 sample 双条件通过
- **Deps**: E2-T07
- **Effort**: L
- **PRD ref**: §4.4

### E4-T04 · `swe_bench_verified` monthly + release
- **Stream**: SUI · SCH
- **Goal**: 月跑 Verified 全量 500 task，release 时也跑
- **Deliverables**: task wrapper + monthly cron + release workflow 集成
- **AC**: release report 中含 Verified pass@1；与 main PRD §11.2 目标对齐（≥ 50%）
- **Deps**: E2-T11
- **Effort**: M
- **PRD ref**: §4.3

### E4-T05 · `aider_polyglot` weekly
- **Stream**: SUI
- **Goal**: 接入 Aider Polyglot 6 语言 × 225 task
- **Deliverables**: task wrapper + weekly cron + per-language F1
- **AC**: 6 语言分独立输出；总 cost ≤ $30/次（Haiku）
- **Deps**: E2-T11
- **Effort**: M
- **PRD ref**: §4.3

### E4-T06 · Trajectory scorer
- **Stream**: SCR
- **Goal**: 建 `evals/scorers/trajectory.py` 算 step / tool_call / retry / restart 统计
- **Deliverables**: scorer + 集成到所有 fix suite
- **AC**: LangSmith sample 字段含 step_count / tool_call_count / retry_count / sandbox_restart_count
- **Deps**: E2-T07
- **Effort**: M
- **PRD ref**: §10.2

### E4-T07 · Online resolution rate ingestion
- **Stream**: OBS
- **Goal**: 给 main PRD §4.2 review pipeline 加埋点；webhook 在 PR merge 时检查 bot 评论附近代码是否被改
- **Deliverables**: `openbot/middleware/resolution_tracker.py` + Postgres `resolution_event` 表 + LangSmith online eval 配置
- **AC**: 一条真实 bot comment 能被追踪到 resolved / unresolved / partial
- **Deps**: 与 main PRD v0.2 release tracker 工作并行
- **Effort**: L
- **PRD ref**: §4 · main PRD §4.2

### 🕒 E4-T08 · `internal_issues_v2` 扩容到 200 · DEFERRED（E2-T03 解冻后再启动）
- **Stream**: DAT
- **Goal**: 把 v1 扩到 200 issue，新增标记 priority/P0..P3
- **Deliverables**: v2 jsonl + manifest（v1 保留不删）
- **AC**: 升版后 triage_internal 同时跑 v1 与 v2，diff 入 baseline-log
- **Deps**: E2-T03
- **Effort**: M
- **PRD ref**: §7.1 §9 dataset 升版

### E4-T09 · `prompt_injection_v2` 扩容到 50+
- **Stream**: DAT
- **Goal**: 加 tool-use chain attack 等高级注入
- **Deliverables**: v2 jsonl + manifest + spot-check
- **AC**: 6 类基础 + 新增 ≥ 3 类高级（chain / multi-turn / encoding）；100% fail-safe
- **Deps**: E2-T12
- **Effort**: M
- **PRD ref**: §4.5 · §13.1

### E4-T10 · `redteam_prompt_injection_xl` task
- **Stream**: SUI
- **Goal**: 用 v2 dataset 的扩容 task
- **Deliverables**: task + release gate 集成
- **AC**: cost ≤ $3/次；release workflow 中替换原 task
- **Deps**: E4-T09
- **Effort**: S
- **PRD ref**: §4.5

---

## Milestone E5 · v0.3 扩展

### 🕒 E5-T01 · `internal_prs_v2` 扩到 100 PR · DEFERRED（E4-T01 解冻后再启动）
- **Stream**: DAT
- **Goal**: 把 E4-T01 的 v1 扩到 100 PR，含 unmatched diff dump 字段
- **Deliverables**: v2 jsonl + manifest + spot-check
- **AC**: 与 v1 并存；样本含 base_sha / head_sha / golden_comments[].severity
- **Deps**: E4-T01
- **Effort**: M
- **PRD ref**: §4.1

### 🕒 E5-T02 · `review_shadow` task · DEFERRED（review gate trip 后启动）
- **Stream**: SUI
- **Goal**: 建 `evals/tasks/review_shadow.py` 接入 `internal_prs_v1`/`v2`
- **Deliverables**: task + smoke + regression + release 三种 mode 配置
- **AC**: 与 review_martian 并列在 release report；G3 shadow hold gate 触发逻辑就位
- **Deps**: E5-T01, E1-T06, E1-T07
- **Effort**: M
- **PRD ref**: §4.1 · §9 G3

### 🕒 E5-T03 · `review_shadow_xl` task · DEFERRED
- **Stream**: SUI
- **Goal**: 用 `internal_prs_v2` 的 release-only suite
- **Deliverables**: task + unmatched diff dump 写 artifact
- **AC**: release workflow 中触发；unmatched diff 可 spot-check
- **Deps**: E5-T02
- **Effort**: S
- **PRD ref**: §4.1

### E5-T04 · `swe_bench_pro` quarterly
- **Stream**: SUI · SCH
- **Goal**: 接入 SWE-bench Pro（含 private 部分），quarterly cron
- **Deliverables**: task wrapper + Scale AI access 文档
- **AC**: 季度跑成本 ≤ $400；含 private 部分作为防训练污染信号
- **Deps**: E2-T07
- **Effort**: L
- **PRD ref**: §4.3 · main PRD §8.2 Verified 诚实声明

### E5-T05 · Diff minimality scorer
- **Stream**: SCR
- **Goal**: 算 patch 中 unrelated change 占比
- **Deliverables**: `evals/scorers/diff_minimality.py` + 集成到 fix suite
- **AC**: P2 指标，不进 gate，但进 release report
- **Deps**: E2-T08
- **Effort**: M
- **PRD ref**: §3.4 P2

### E5-T06 · LangSmith annotation queue 接入
- **Stream**: GOV · OBS
- **Goal**: 把人工 spot-check 流程接到 LangSmith annotation queue
- **Deliverables**: 配置 + `docs/eval/annotation-process.md`
- **AC**: 至少 20 个 sample 走完 annotation 流；与 baseline 校准
- **Deps**: E2-T02
- **Effort**: L
- **PRD ref**: §18 Open Q1

---

## 跨 milestone · 持续维护任务

### CR-T01 · Judge model 升级流程演练
- **Stream**: GOV
- **Goal**: 在 E2 完成后做一次模拟升级（如 Opus 4.7 → 4.7.1）演练 §10.3 流程
- **Deliverables**: `docs/eval/judge-version-log.md` 第一条真实条目（即使是同版本回归）
- **AC**: 演练后流程文档化（含回滚步骤）
- **PRD ref**: §10.3

### CR-T02 · Baseline 月度审计
- **Stream**: GOV
- **Goal**: 每月 review baseline-log，识别漂移
- **Deliverables**: `docs/eval/baseline-audit-YYYY-MM.md`
- **AC**: 偏离 ≥ 2σ 的指标列入 follow-up
- **PRD ref**: §15.1

### CR-T03 · Open Question Q1-Q5 决议
- **Stream**: GOV
- **Goal**: PRD §18 五个 open question 在 E2 完成前 resolve
- **Deliverables**: 每个 Q 一条 PR 修改 PRD 或新建 ADR
- **AC**: 5 个 Q 全部有 close 状态
- **PRD ref**: §18

### CR-T04 · 公开 benchmark upstream 锁版本
- **Stream**: INF
- **Goal**: 每个公开 benchmark 用到的 upstream commit hash 都进 manifest
- **Deliverables**: `evals/datasets/manifests/upstream-pins.yaml`
- **AC**: 升级走 PR + baseline 重跑
- **PRD ref**: §16 风险表 · §17 #6

---

## 关键依赖图

```
E0-T02 (skeleton) ──┬─► E1-T01 (inspect-ai dep) ──► E1-T02 (langsmith) ──► E1-T03..T05 ──► E1-T06..T08
                    │                                                                            │
                    └─► E0-T03..T05 (governance / budget templates)                              │
                                                                                                 ▼
E1 closed loop ─────► E2 suites (martian/triage/fix/redteam) ─────► E3 scheduler/reporting ─────► E4 expansion ──► E5
                                  │
                                  └─► E2-T11 swe_bench_lite (depends on Modal sandbox + fix solver)
```

---

## 任务统计

### Active（v0.1 / v0.2 实际要做）

| Milestone | active 任务数 | deferred | 总 effort |
|---|---|---|---|
| E0 | 5 | 0 | ≈ 2.5 day |
| E1 | 10 | 0 | ≈ 8 day |
| E2 | 10 | 6 🕒 | ≈ 11 day（去掉 internal triage/fix smoke 后） |
| E3 | 9 | 0 | ≈ 9 day |
| E4 | 8 | 2 🕒 | ≈ 11 day |
| E5 | 3 | 3 🕒 | ≈ 5 day |
| CR | 5（新增 CR-T05） | 0 | 持续 |
| **active 合计** | **50** | **11 🕒** | **~46 day** |

### Deferred（11 条，等 §4.0 gate trip 后启动）

合并启动后估计 effort：~14 day（dataset 构建是主要工作量）

### 对比

| 维度 | 调整前 | 调整后 |
|---|---|---|
| 总任务 | 60 | 50 active + 11 deferred = 61 |
| v0.1 alpha effort | ~26.5 day（E0+E1+E2） | **~21.5 day** |
| v0.1 review 信号 | shadow + martian 双轨 | **martian 单轨** |
| v0.1 triage 信号 | triage_internal | **不承诺** |
| v0.1 fix 信号 | swe_bench_lite + fix_internal_smoke | **swe_bench_lite 单轨** |

省下来的 ~5 day 可以投到 main PRD v0.1 alpha 的 4 个产品 feature 上。

---

## 使用建议

### 批量 issue 化

```bash
# 示例：把本文件中所有 E1 任务批量开成 issue（伪命令，需自行适配 parser）
grep -E "^### E1-T[0-9]+" docs/eval/task-list.md \
  | while read -r line; do
      title=$(echo "$line" | sed 's/^### //')
      gh issue create --title "$title" --body "见 docs/eval/task-list.md 中对应任务" --label "eval,milestone-E1"
    done
```

### PR 描述模板

每个 PR 解决一条任务时，在描述中加：

```
Closes <issue-link>
Task: E{X}-T{NN}
PRD ref: §<section>
AC checked:
  - [x] <criterion 1>
  - [x] <criterion 2>
```

### 进度回写

milestone 完成后在 `docs/eval/baseline-log.md` 写 milestone 收尾 entry，含本期所有 suite baseline 分数与 cost。

---

## 与 main PRD 的对齐

| Eval milestone | OpenBot 版本节点 | 承诺的 KPI（仅公开信号） |
|---|---|---|
| E0-E1 | v0.1 alpha Week 1-2 | — |
| E2 (active 部分) | v0.1 alpha 出货前 | SWE-bench Lite ≥ 50% · Martian F1 ≥ 0.35 · redteam 100% |
| E3 | v0.1 → v0.2 过渡 | 周跑趋势告警就位 |
| E4 (active 部分) | v0.2 完整 MVP | Verified ≥ 50% · Polyglot baseline · GitBugs F1 baseline |
| E5 (active 部分) | v0.3 | annotation queue · Pro 信号 |
| E-Internal | gate trip 后异步插入 | 解冻当下 release 加报对应 internal 信号 |

**重要**：eval milestone 不可领先 OpenBot 版本节点 —— 没有真实 workflow 就跑不出真实 baseline；同理，没有真实 dogfood 数据就不建 internal dataset。

**Internal-signal 何时对外承诺？** 任一 gate trip 后，下一个 release 起开始报；在此之前 OpenBot 对外只承诺 §15.2 表中"公开"那几行。
