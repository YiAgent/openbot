"""Budget guardrail constants for OpenBot evals.

Values are locked to eval PRD §11.1 (per_sample_usd / per_suite_run_usd /
monthly_total_usd). Any change here is a governance event — it MUST update
both the PRD § and `docs/eval/baseline-log.md`.

Reference: docs/prd/openbot-eval-prd.md §11 Cost & Budget Guardrails.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

# eval PRD §11.1 — per-sample cost ceiling, suite-keyed.
# `failure_category=budget_stop` fires when a single sample exceeds 1.2x its
# entry here (PRD §11.2).
PER_SAMPLE_USD: Final[Mapping[str, float]] = MappingProxyType(
    {
        "review_shadow": 0.50,
        "review_martian": 0.60,
        "triage_internal": 0.20,
        "fix_internal_smoke": 3.00,
        "swe_bench_lite": 1.50,  # 单 instance 上限
        "redteam_prompt_injection": 0.05,
    }
)

# eval PRD §11.1 — per-suite-run cost ceiling, mode-keyed.
# Hitting the ceiling aborts the whole run with an audit log (PRD §11.2).
PER_SUITE_RUN_USD: Final[Mapping[str, float]] = MappingProxyType(
    {
        "smoke": 5,
        "regression": 30,
        "weekly": 200,
        "release": 800,
    }
)

# eval PRD §11.1 — month-wide cap across all eval modes.
# Going over pauses all eval workflow_dispatch until
# `openbot eval budget reset` is run by a maintainer (PRD §11.2).
MONTHLY_TOTAL_USD: Final[float] = 1500
