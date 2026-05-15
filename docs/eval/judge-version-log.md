# Eval · Judge Model / Prompt Version Log

> 上位文档：[`openbot-eval-prd.md` §10.3 Judge model / prompt 治理](../prd/openbot-eval-prd.md#103-judge-model--prompt-治理)
> 用途：把 judge LLM 的**每一次**升级（model 版本 / prompt 改动 / scorer 逻辑改动）的前后分数差异写下来，使 release notes 能复现，且事后能解释「为什么 baseline 变了」。

## 如何记录

每行 = 一次 judge 相关改动事件。**触发条件**（任一即必须新增一行）：

1. **Judge model 升级** — 例如 `anthropic/claude-opus-4-7` → `anthropic/claude-opus-4-8`。
2. **Judge prompt 改动** — PR 改 `evals/common/judges.py`。
3. **Scorer 逻辑改动** — PR 改 `evals/scorers/*`。

记录字段（**全部必填**）：

| 字段 | 说明 |
|---|---|
| `date` | YYYY-MM-DD |
| `change_type` | judge-model / judge-prompt / scorer-logic |
| `prev` | 例 `judge_model_id=anthropic/claude-opus-4-7` 或 `judge_prompt_version=3` 或 `scorer=review_overlap@v2` |
| `new` | 同上格式 |
| `rationale` | 为什么升级（PRD §10.3 第一栏「PR 描述 rationale」） |
| `release_suite_run` | 触发的全部 release suite 重跑实验名（PRD §10.4 命名） |
| `score_diff` | 表格：每个 release suite 的主指标 prev → new，含 ↑/↓ 标记 |
| `acceptance` | maintainer-approved / rejected / rolled-back，附 approver |
| `pr_url` | 触发 PR |
| `linked_baseline_entry` | 对应 `docs/eval/baseline-log.md` 的 `date + suite` 入口（每条 score 行必须有 baseline-log 对应） |
| `notes` | 异常解释 / 后续 follow-up issue |

约束：
- **append-only**；rollback 时新增一行 `change_type=rollback`，`prev`/`new` 互换，并在 `notes` 里指明 supersedes 哪条。
- `release_suite_run` 必须**覆盖 PRD §9 表里所有 release-tier suite**（v0.1：`review_martian` + `swe_bench_lite` + `redteam_prompt_injection`）。漏一个就视为未完成升级流程，maintainer 应拒绝合并。
- **Judge 锁定到 release**（PRD §10.3 末段）：每次 release notes 必须引用本日志当前生效的 `judge_model_id` + `judge_prompt_version`；本日志缺记录 = release notes 不能签发。
- 升级路径不允许跳号（避免「v4 升到 v6 怎么对齐 baseline」的歧义）。如 prompt v3 → v5，写两条：v3→v4、v4→v5。

### Schema 示例

```yaml
- date: 2026-07-12
  change_type: judge-model
  prev: judge_model_id=anthropic/claude-opus-4-7
  new: judge_model_id=anthropic/claude-opus-4-8
  rationale: "4.8 在 chain-of-thought review 上更稳；prev model deprecation 公告 2026-09-01"
  release_suite_run:
    - "review-martian-v1-x1y2z3-opus48-release"
    - "swe-lite-upstream2026w28-x1y2z3-opus48-release"
    - "redteam-prompt_injection_v1-x1y2z3-opus48-release"
  score_diff:
    review_martian:    { f1: "0.62 → 0.65 (↑)" }
    swe_bench_lite:    { resolved_pct: "32.0 → 33.2 (↑)" }
    redteam_pi:        { fail_safe_pct: "100.0 → 100.0 (–)" }
  acceptance: approved-by: @yiwang
  pr_url: "https://github.com/YiAgent/openbot/pull/456"
  linked_baseline_entry:
    - "baseline-log.md@2026-07-12 review_martian"
    - "baseline-log.md@2026-07-12 swe_bench_lite"
    - "baseline-log.md@2026-07-12 redteam_prompt_injection"
  notes: "无回归；redteam fail-safe 仍 100%。"
```

## Entries

<!-- placeholder：首条 judge entry 应由首次 release（v0.1.0）写入，锁定初版 judge model/prompt。 -->
- _(placeholder) — 首条 judge entry 将由 v0.1.0 release 落地（确定 `judge_model_id` + `judge_prompt_version=1`）。在此之前本表为空。_
