# `evals/` — OpenBot Eval Workspace

> 上位文档：[`docs/prd/openbot-eval-prd.md`](../docs/prd/openbot-eval-prd.md) · [`docs/eval/task-list.md`](../docs/eval/task-list.md)
> Locked runner: **Inspect AI** · Locked observability: **LangSmith** · Locked sandbox: **Modal**（内部）/ **Inspect Docker**（公开 benchmark）

`evals/` 是 Inspect AI 跑 eval 的根目录。任何 LLM-behavior / prompt-quality 断言都进这里，**不要**放到 `tests/`（PRD §8.3）。

## 目录结构（PRD §6.1）

```
evals/
├── README.md               ← 本文件
├── common/                 ← langsmith / metadata / config / artifacts / judges
├── solvers/                ← openbot_review / triage / fix / redteam 等 solver
├── scorers/                ← review_overlap / triage_labels / patch_tests / reproducer / safety / trajectory
├── datasets/               ← *.jsonl 数据文件
│   └── manifests/          ← 每条 dataset 的 *.yaml manifest（schema 见 eval PRD §7.2）
├── tasks/                  ← Inspect task 入口（一个 file = 一个 suite）
└── scripts/                ← build_* / export_run_summary / compare_runs
```

PR 改动 `evals/tasks/` · `evals/scorers/` · `evals/datasets/` · `evals/solvers/` 任一文件 → 强制触发对应 regression suite（eval PRD §8.2 trigger 表）。

## v0.1 / v0.2 suite 列表

下表覆盖 eval PRD §4.0 范围调整后**实际计划构建**的 suite。`Status` 标 🕒 的为 **DEFERRED**（等 §4.0 gate trip 后再做），E0 阶段仅占位、不创建对应文件。

| Suite | Stream | Status | Task ID | 数据源 |
|---|---|---|---|---|
| `review_martian` | REVIEW (公开 baseline) | active | E2-T01 / T02 | Martian upstream 5–50 samples |
| `swe_bench_lite` | FIX (公开 benchmark) | active | E2-T11 | `inspect_evals/swe_bench_lite`（commit hash 锁定） |
| `redteam_prompt_injection` | SAFETY (hard gate) | active | E2-T12..T14 | 手写 prompts，不依赖真实数据 |
| `redteam_prompt_injection_xl` | SAFETY (扩容) | active | E4-T10 | 手写 prompts |
| `libro_reproducer` | FIX 辅助 | active | E4-T03 | 公开 LIBRO benchmark |
| `swe_bench_pro` | FIX quarterly | active | E5-T04 | 公开 benchmark |
| 🕒 `internal_issues_v1` (triage) | TRIAGE | deferred | E2-T03..T06 | 解冻：≥ 50 closed issue 含 label/priority/dup-of |
| 🕒 `fix_internal_smoke` | FIX | deferred | E2-T09 / T10 | 解冻：≥ 10 issue→PR retrospective |
| 🕒 `internal_prs_v1` | REVIEW shadow | deferred | E4-T01 | 解冻：≥ 30 PR 含 ≥ 2 reviewer comments |
| 🕒 `internal_issues_v2` | TRIAGE 扩容 | deferred | E4-T08 | triage gate trip 之后 |
| 🕒 `internal_prs_v2` | REVIEW 扩容 | deferred | E5-T01 | review gate trip 之后 |
| 🕒 `review_shadow` | REVIEW shadow | deferred | E5-T02 | review gate trip |
| 🕒 `review_shadow_xl` | REVIEW shadow release | deferred | E5-T03 | review gate trip |

完整解冻条件见 eval PRD §4.0；E0 阶段不创建 🕒 suite 的 task 文件，但本目录树足以承接它们解冻后的代码落点。

## 怎么跑（E0 阶段：占位 only）

E0 milestone 只建目录骨架；真正可跑的命令在 E1 之后逐 task 上线（见 `docs/eval/task-list.md`）：

- **本地 smoke**（E1-T07 之后可用）：`inspect eval evals/tasks/review_martian.py --limit 5`
- **公开 benchmark**（E2-T11 之后）：`inspect eval inspect_evals/swe_bench_lite --solver evals.solvers.openbot_fix`
- **CI 触发**（E3 之后）：PR matcher / cron 由 `.github/workflows/eval.yml` 调度
- **LangSmith 校验**（每次 run）：`python scripts/validate_langsmith_run.py <run_id>`（E0-T04 占位、E1-T02 起填充）

## 边界提示

- `evals/` 不被 pytest 收集：`Makefile` 的 `test` target 与 `scripts/hooks/pytest-pre-push.sh` 均带 `--ignore=evals`。
- Budget 常量（`PER_SAMPLE_USD` / `PER_SUITE_RUN_USD` / `MONTHLY_TOTAL_USD`）将落在 `evals/common/config.py`（E0-T05）。
- 治理日志（baseline / judge version / flaky samples / dataset spot-check）在 `docs/eval/` 下，由 E0-T03 建立模板。
