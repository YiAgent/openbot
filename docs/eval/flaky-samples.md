# Eval · Flaky Samples Registry

> 上位文档：[`openbot-eval-prd.md` §12.2 Flake 处理](../prd/openbot-eval-prd.md#122-flake-处理) · [`§12.4 failure_category 枚举`](../prd/openbot-eval-prd.md#124-failure_category-固定枚举)
> 用途：登记那些被自动归类为 `flaky` 的 sample——它们**仍保留 raw record，但从聚合 metric 剔除**。每月人工 review 一次。

## 如何记录

「flaky」的自动门槛（PRD §12.2）：同一 sample 在**最近 3 次 run** 内 ≥ 2 次落入 `failure_category=transient_*`（参 §12.4：`transient_network` / `transient_modal` / `transient_litellm` / 其它前缀 `transient_`）。
触发后，由 `scripts/export_run_summary.py`（E3 milestone）自动追加一行；本文件**也可以被人手编辑**——人工 spot-check 后确认 / 撤销 flaky 标记。

记录字段（**全部必填**）：

| 字段 | 说明 |
|---|---|
| `first_detected` | YYYY-MM-DD（第一次被自动标 flaky 的 run） |
| `sample_id` | dataset 内唯一 ID（PRD §10.2） |
| `suite` | 例 `review_martian` |
| `dataset_version` | 例 `martian_review_v1` |
| `transient_categories` | 出现过的 `failure_category` 列表 |
| `recent_runs` | 最近 3 次 run 在该 sample 上的结果（pass / fail / transient） |
| `last_reviewed` | YYYY-MM-DD；月度 review 时间 |
| `status` | flaky-active / quarantined / fixed / dropped |
| `owner` | 谁负责跟（issue/PR 链接或 maintainer handle） |
| `notes` | 为什么 flaky；上下游 bug 链接；是否计划删除/替换 sample |

约束：
- **每月一次人工 review**（PRD §12.2 末句）。月度 review 把所有 `status=flaky-active` 重新分类为 fixed / dropped / quarantined / 仍 flaky-active；更新 `last_reviewed`。
- `status=fixed` 的 sample 不删行——保留作历史；下次再 flaky 直接新建一行（不要复用旧条目）。
- `status=dropped` 必须在 `notes` 引用 dataset 升版 PR（dataset 必须升版，不能就地删 sample——PRD §7.1 ②「冻结输入」）。
- Suite-level abort（`transient` 占比 > 20%，PRD §12.3）发生时，**不**逐 sample 入表；改在 `docs/eval/baseline-log.md` 上记录整 run abort。

### Schema 示例

```yaml
- first_detected: 2026-06-08
  sample_id: martian-pr-#421
  suite: review_martian
  dataset_version: martian_review_v1
  transient_categories: [transient_modal, transient_network]
  recent_runs:
    - { run_id: "review-martian-...-weekly-2026w22", outcome: transient_modal }
    - { run_id: "review-martian-...-weekly-2026w23", outcome: pass }
    - { run_id: "review-martian-...-weekly-2026w24", outcome: transient_modal }
  last_reviewed: 2026-07-01
  status: flaky-active
  owner: "@yiwang (issue #789)"
  notes: "Modal 该 sample clone 体积过大；考虑 v2 dataset 缩小 fixture。"
```

## Monthly review checklist

每月 1 号（或最近工作日）：

- [ ] 把所有 `status=flaky-active` 走一遍：仍 flaky / 已 fix / 应 drop？
- [ ] 更新每行的 `last_reviewed`。
- [ ] 如有 `dropped`，确认 dataset 升版 PR 已开。
- [ ] 检查 raw run 里 `transient_*` 占比趋势——是否需要降阈值（PRD §12.3 的 20% suite abort 线）。

## Entries

<!-- placeholder：首条 flaky entry 由 E1 之后第一次跑 regression 的 export_run_summary 自动追加。E0 阶段仅占位。 -->
- _(placeholder) — 首条 flaky entry 将由 E3 milestone（`scripts/export_run_summary.py` 接入后）的首个 weekly run 自动写入。在此之前本表为空。_
