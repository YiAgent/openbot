# Eval · Baseline Log

> 上位文档：[`openbot-eval-prd.md` §9 Gating Policy](../prd/openbot-eval-prd.md#9-gating-policysolo-表)
> 用途：每次 release baseline 重跑或 dataset 升版的**审计轨迹**。任何 gate（G1/G2/G3/G4/G5/G10）的「baseline」字段都指向这本日志里的一行。

## 如何记录

每行 = 一次 baseline 重跑事件。**触发条件**（任一即必须新增一行）：

1. **新 release 发版** — 例行刷 baseline。
2. **Judge model 升级**（PRD §10.3，例如 Opus 4.7 → 4.8）。
3. **Judge prompt 改动**（PR 改 `evals/common/judges.py`）。
4. **Scorer 逻辑改动**（PR 改 `evals/scorers/*`）。
5. **Dataset 升版**（v{N} → v{N+1}；按 PRD §9 "Baseline 治理" 必须**同时**跑 v{N} 与 v{N+1} 并解释 diff，再切换 baseline 引用）。

记录字段（**全部必填**，与 LangSmith `experiment_tag=baseline-release-<version>` 对得上）：

| 字段 | 说明 |
|---|---|
| `date` | YYYY-MM-DD |
| `release_version` | 触发此次 baseline 的 release tag；非 release（如 judge 升级）写 `interim-<git_sha_short>` |
| `trigger` | release / judge-model / judge-prompt / scorer-logic / dataset-bump |
| `suite` | 重跑的 suite 名（一行一个 suite；多个 suite 同时刷写多行） |
| `dataset_version` | 例 `internal_prs_v1` |
| `judge_model_id` · `judge_prompt_version` | 取自 LangSmith run metadata（PRD §10.1） |
| `prev_score` · `new_score` · `delta` | 主指标值与差异（F1 / resolved% / fail-safe%；见 §9 阈值列） |
| `gate_status` | pass / soft-warn (G1/G5) / hard-block (G2/G3/G6) |
| `langsmith_experiment` | 实验名 `{suite}-{dataset_version}-{git_sha_short}-{model_alias}-{mode}`（PRD §10.4） |
| `pr_url` | 触发本次 baseline 的 PR，链回 LangSmith run id |
| `notes` | diff 解释、人工审定结论、follow-up issue 链接 |

约束：
- **append-only**；不要回头改旧行。修正用一行新 entry，`notes` 里指明 supersedes `<date>` 那条。
- 触发 4 / 5（scorer / dataset 改动）时，`notes` 字段必须解释「旧分数为什么会 drift」，否则 reviewer 应该 block PR。
- 每行的 `langsmith_experiment` 必须**能在 LangSmith UI 里找到**；空字符串视为本行无效。
- Judge / scorer / dataset 三类改动**同时发生**时，拆成多行——每条变更轴各占一行，便于事后归因。

### Schema 示例

```yaml
- date: 2026-05-15
  release_version: v0.1.0
  trigger: release
  suite: review_martian
  dataset_version: martian_review_v1
  judge_model_id: anthropic/claude-opus-4-7
  judge_prompt_version: 1
  prev_score: null            # 首发，无前值
  new_score: { f1: 0.62 }
  delta: null
  gate_status: pass
  langsmith_experiment: "review-martian-v1-a1b2c3-opus47-release"
  pr_url: "https://github.com/YiAgent/openbot/pull/123"
  notes: "首次 release baseline 锚点。"
```

## Entries

<!-- placeholder：第一行 baseline 应由 v0.1.0 release 那次刷 baseline 的 PR 补齐。E0 阶段仅占位。 -->
- _(placeholder) — 首条 baseline 将由 v0.1.0 release（E3 milestone 之后）写入。在此之前本表为空。_
