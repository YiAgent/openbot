# OpenBot Eval · Suite 详细定义

> 范围：v0.1 ~ v0.3 每业务评测套件 (Suites) 硬约束

## 1. Triage (分诊)

| Suite | 阶段 | 数据源 | 核心指标 | Floor | 备注 |
|---|---|---|---|---|---|
| `gitbugs` | v0.1 | GitBugs (200 samples) | `macro_f1` | 0.55 | 6 类标签归一化匹配 |
| `triage_internal_v1` | v0.2 | 历史自跑数据 (≥ 200) | `macro_f1` | 0.65 | Ground truth 为人工修正后标签 |
| `triage_internal_online` | v0.3 | 生产实时流 | `macro_f1_30d` | 0.70 | LangSmith Online Eval 接管 |

---

## 2. Review (代码评审)

| Suite | 阶段 | 数据源 | 核心指标 | Floor | 备注 |
|---|---|---|---|---|---|
| `review_codereviewbench` | v0.1 | Martian CRB (50 PR) | `mean_f1` | 0.55 | LLM-judge 语义匹配 |
| `review_internal_v1` | v0.2 | 人工标注 (≥ 200) | `useful_rate` | 0.65 | 三分类: useful / noise / wrong |
| `review_martian_online` | v0.3 | Martian Live Stream | `action_rate` | 0.40 | 统计 comment 被采纳/resolve 率 |

---

## 3. Fix (缺陷修复)

| Suite | 阶段 | 数据源 | 核心指标 | Floor | 备注 |
|---|---|---|---|---|---|
| `fix_swe_bench_verified` | v0.1 | SWE-bench Verified | `pass@1` | 0.40 | **Offline Grading** (JSONL export) |
| `test_swt_bench_verified` | v0.1 | SWT-Bench Verified | `success_rate` | baseline | Fix 辅助诊断；要求写出回归测试 |
| `fix_internal_v1` | v0.2 | 内部 PR 历史 (≥ 200) | `pass@1` | 0.50 | Modal 沙箱重跑验证 |
| `fix_swe_bench_live` | v0.3 | SWE-bench Live | `pass@1_90d` | 0.35 | 滚动 90 天月度发布数据 |

---

## 4. Chat (代码问答)

| Suite | 阶段 | 数据源 | 核心指标 | Floor | 备注 |
|---|---|---|---|---|---|
| `chat_swe_qa_pro` | v0.1 | SWE-QA-Pro (260) | `normalized` | 0.65 | 5-dim judge (Appendix D) |
| `chat_internal_v1` | v0.2 | 真实 @bot 问答 (≥ 200) | `correct_rate` | 0.70 | 人工标注 correctness |
| `chat_internal_online` | v0.3 | 生产采样 | `correct_30d` | 0.75 | 结合 follow-up 率与 thumbs 信号 |

---

## 5. Safety (安全门禁)

| Suite | 阶段 | 规模 | 核心指标 | Target | 备注 |
|---|---|---|---|---|---|
| `redteam_v0` | v0.1 | 24 prompts | `mean_safe` | **1.00** | 注入检查；Block merge |
| `redteam_v1` | v0.2 | 100+ prompts | `mean_safe` | **1.00** | 增加 C&C 及间接注入变体 |
| `threat_intel` | v0.3 | 滚动增长 | `mean_safe` | **1.00** | CVE/Security RSS 实时转入 |
