"""Martian review eval task — PRD §4.1 / §14 E1 验收.

**E1-T08 SKELETON ONLY.** Blocked on:
  - E1-T06 (`openbot_review_solver` still raises NotImplementedError)
  - E1-T04 (artifact export still ❌ blocked)
  - E0-T04 ✅ (validator script in place)

When unblocked, this file should expose an inspect-ai `@task` function that:
  1. Loads Martian upstream 5-50 PR samples (E2-T01 will lock the dataset commit).
  2. Wires `openbot_review_solver()` (E1-T06) into the inspect-ai pipeline.
  3. Scores with `review_overlap_scorer()` (E1-T07).
  4. Records run-level metadata via `evals.common.metadata.collect_run_metadata`
     and per-sample fields via `evals.common.langsmith.log_sample`.

Until then, `martian_review_task()` raises with a pointer to the handoff.
"""

from __future__ import annotations

from typing import Any


def martian_review_task() -> Any:
    """Inspect AI @task placeholder for the Martian review smoke run."""
    raise NotImplementedError(
        "E1-T08 Martian smoke task is blocked: needs E1-T06 review solver "
        "and E1-T04 artifact export. See docs/eval/handoffs/E1-T08.md."
    )
