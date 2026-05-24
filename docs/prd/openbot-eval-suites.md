# OpenBot Eval · Suite 详细定义

> 版本：**3.0 Current-State Redesign** · 最近更新：2026-05-22
> 范围：v0.1 alpha ~ v0.3 每业务评测套件硬约束
> 主 PRD：[`openbot-eval-prd.md`](./openbot-eval-prd.md)

## 0. Suite 命名原则

Suite 名称描述 benchmark / dataset；solver 名称描述 OpenBot 能力。

```text
tasks/review_martian.py        -> solvers/review.py          -> openbot.evaluation.run_review_sample
tasks/fix_swe_bench.py         -> solvers/fix.py             -> openbot.evaluation.run_fix_sample
tasks/chat_swe_qa.py           -> solvers/chat.py            -> openbot.evaluation.run_chat_sample
tasks/test_swt_bench.py        -> solvers/test_generation.py -> openbot.evaluation.run_test_generation_sample
```

禁止使用 `deepagents_baseline_*`、`*_deepagents`、`*_openbot_prod` 作为长期 suite / solver 名称。

---

## 1. v0.1 Alpha Suites

| Suite | Task file | Dataset | Product capability | Primary metric | Floor / gate | Status |
|---|---|---|---|---|---|---|
| `review_martian` | `tasks/review_martian.py` | Martian CRB mirror (`martian_2026w20`) | Review | mean F1 | smoke 必过；regression 相对下降 ≥ 10% block | 保留并改造 |
| `fix_swe_bench` | `tasks/fix_swe_bench.py` | SWE-bench Verified | Fix | valid prediction export；official pass@1 offline | JSONL schema 必过；pass@1 release 观察 | 保留并改造 |
| `chat_swe_qa` | `tasks/chat_swe_qa.py` | SWE-QA-Pro mirror (`chat_swe_qa_pro_v1`) | Chat | 5-dim normalized score | judge result 必须存在；alpha 阶段 warn | 保留并改造 |
| `test_swt_bench` | `tasks/test_swt_bench.py` | SWT-Bench Verified | Test generation | valid unsupported/export metadata | 产品能力未实现前必须 `unsupported=true` | surface 保留 |

---

## 2. Review

| Suite | 阶段 | 数据源 | 核心指标 | Floor | 备注 |
|---|---|---|---|---|---|
| `review_martian` | v0.1 | Martian CRB mirror | `mean_f1`, `precision`, `recall` | smoke 可跑；regression 相对下降 ≥ 10% block | LLM-judge 语义匹配；调用 OpenBot review 产品路径 |
| `review_internal_v1` | v0.2 | 人工标注 PR 历史 ≥ 200 | `useful_rate`, `wrong_rate` | TBD when dataset exists | 三分类: useful / noise / wrong |
| `review_online` | v0.3 | 生产 PR review 流 | `action_rate`, `resolved_rate` | 趋势观察 | 统计 bot comment 被采纳 / resolve 率 |

Review scorer 接受统一 finding shape：

```text
file: str
line: int | None
body: str
severity: "low" | "medium" | "high"
```

OpenBot severity 映射：

| OpenBot | Eval |
|---|---|
| `critical` | `high` |
| `high` | `high` |
| `medium` | `medium` |
| `low` | `low` |
| `nit` | `low` |

---

## 3. Fix

| Suite | 阶段 | 数据源 | 核心指标 | Floor | 备注 |
|---|---|---|---|---|---|
| `fix_swe_bench` | v0.1 | SWE-bench Verified | `valid_prediction`, official `pass@1` offline | JSONL schema 必过；pass@1 release 观察 | Inspect 只导出 prediction；官方 harness 离线评分 |
| `fix_internal_v1` | v0.2 | 内部 issue / PR 历史 ≥ 200 | `pass@1`, `unrelated_change_rate` | dataset 建立后定义 | 调 OpenBot 产品 harness；不走 eval sandbox |
| `fix_swe_bench_live` | v0.3 | SWE-bench Live / rolling public set | `pass@1_90d` | 趋势观察 | 月度发布数据 |

Fix eval 只需要 patch prediction，不允许在 offline benchmark 中真的 push branch 或 open PR。OpenBot evaluation facade 负责使用产品 sandbox / checkout / fix responder，并返回 `model_patch`。

---

## 4. Chat

| Suite | 阶段 | 数据源 | 核心指标 | Floor | 备注 |
|---|---|---|---|---|---|
| `chat_swe_qa` | v0.1 | SWE-QA-Pro mirror | 5-dim normalized score | alpha 阶段 warn | 调 OpenBot chat 产品路径；不添加 eval-only repo tools |
| `chat_internal_v1` | v0.2 | 真实 @bot 问答 ≥ 200 | `correct_rate` | dataset 建立后定义 | 人工标注 correctness |
| `chat_online` | v0.3 | 生产采样 | `correct_30d`, `followup_rate` | 趋势观察 | 结合 thumbs / follow-up 信号 |

如果当前产品 chat 没有 repo-grounded tools，eval 必须如实反映这个能力缺口。不能在 `evals/` 中补一套 read/search tools。

---

## 5. Test Generation

| Suite | 阶段 | 数据源 | 核心指标 | Floor | 备注 |
|---|---|---|---|---|---|
| `test_swt_bench` | v0.1 | SWT-Bench Verified | `unsupported_metadata` until implemented | 必须显式 `unsupported=true` | 产品侧无正式 test-generation agent 前不跑旧 eval-only agent |
| `test_swt_bench` | v0.2+ | SWT-Bench Verified | official success / coverage | capability 实现后定义 | 一旦 OpenBot 产品侧有正式 test-generation responder，再转为真实评分 |

当前锁定：不保留 `evals.agents.test_generation`。产品能力未实现时输出空 validated prediction，并在 LangSmith metadata 写：

```text
unsupported = true
unsupported_reason = "not_implemented"
```

---

## 6. Triage

| Suite | 阶段 | 数据源 | 核心指标 | Floor | 备注 |
|---|---|---|---|---|---|
| `triage_gitbugs` | v0.2 candidate | GitBugs subset | `macro_f1` | TBD | 等产品 triage 输出 label / priority 后接入 |
| `triage_internal_v1` | v0.2 | 历史 issue ≥ 200 | `macro_f1`, `priority_accuracy` | dataset 建立后定义 | Ground truth 为人工修正后标签 |
| `triage_online` | v0.3 | 生产实时流 | `macro_f1_30d` | 趋势观察 | LangSmith online eval / annotation queue |

Triage 不属于当前 eval runtime 重构的 v0.1 alpha acceptance，因为产品侧 triage 尚未完成。

---

## 7. Safety

| Suite | 阶段 | 规模 | 核心指标 | Target | 备注 |
|---|---|---|---|---|---|
| `redteam_v0` | v0.1+ | 24 prompts | `mean_safe` | 1.00 | 注入检查；后续接入 OpenBot 产品路径 |
| `redteam_v1` | v0.2 | 100+ prompts | `mean_safe` | 1.00 | 增加间接注入、secret exfiltration、tool misuse |
| `threat_intel` | v0.3 | 滚动增长 | `mean_safe` | 1.00 | CVE / Security RSS 实时转入 |

Safety suite 不得调用 eval-only tools。它必须通过 OpenBot 的产品 reply/review/fix 输出边界验证泄漏和注入处理。

---

## 8. LangSmith 输出要求

每个 sample 的 LangSmith experiment row 必须包含：

```text
dataset_version
solver_family = "openbot_agent"
solver_id
capability
openbot_git_sha
mode
```

Review / Chat 必须写 judge feedback。Fix / Test 必须写 prediction export status。Unsupported test generation 必须写 `unsupported=true`。

---

## 9. v0.1 验收清单

| Check | Expected |
|---|---|
| `review_martian` one-sample smoke | 通过，并上传 LangSmith feedback |
| `fix_swe_bench` one-sample smoke | 产出 valid SWE prediction JSONL |
| `chat_swe_qa` one-sample smoke | 产出 answer + judge feedback |
| `test_swt_bench` one-sample smoke | 产出 `unsupported=true` metadata |
| `rg "evals\\.agents|evals\\.sandboxes|deepagents_baseline" evals tests/eval` | 无 live hits |
| `uv run pytest tests/eval tests/evaluation` | 通过 |
