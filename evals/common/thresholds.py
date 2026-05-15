"""PRD §9 Gating Policy thresholds — verbatim mirror.

This module is the single source of truth that ``evals.scripts.compare_runs``
and any future gate-enforcing tooling consults. Any value here MUST match
``docs/prd/openbot-eval-prd.md`` §9 character-for-character; the test suite
pins these numbers so an accidental drift fails CI.

Naming convention: ``G<n>_<short>`` for thresholds, ``G<n>_BEHAVIOR`` for
the documented response (soft warn / hard block).
"""

from __future__ import annotations

from typing import Final, Literal

GateBehavior = Literal["soft-warn", "hard-block", "alert", "block-release"]


# --- G1 / G2 — Martian Code Review regression -------------------------------
# Metric: Martian Code Review F1 vs last release baseline.
G1_REVIEW_SOFT_F1_DROP: Final[float] = 0.05  # ≥ 5% drop ⇒ PR comment warning
G2_REVIEW_HARD_F1_DROP: Final[float] = 0.10  # ≥ 10% drop ⇒ block merge
G1_BEHAVIOR: Final[GateBehavior] = "soft-warn"
G2_BEHAVIOR: Final[GateBehavior] = "hard-block"

# --- G3 — Shadow hold -------------------------------------------------------
# Metric: shadow-set precision + recall vs last release. Any drop ⇒ block.
G3_BEHAVIOR: Final[GateBehavior] = "block-release"

# --- G4 — SWE-bench Lite weekly drift ---------------------------------------
G4_LITE_RESOLVED_DROP: Final[float] = 0.03  # ↓ ≥ 3% week-over-week
G4_BEHAVIOR: Final[GateBehavior] = "alert"

# --- G5 — Triage label F1 ---------------------------------------------------
G5_TRIAGE_LABEL_F1_MIN_V0_1: Final[float] = 0.55  # < 0.55 ⇒ soft warn (v0.1)
G5_TRIAGE_LABEL_F1_MIN_V0_2: Final[float] = 0.65  # < 0.65 ⇒ soft warn (v0.2)
G5_BEHAVIOR: Final[GateBehavior] = "soft-warn"

# --- G6 — Safety hard gate --------------------------------------------------
G6_SAFETY_FAIL_SAFE_MIN: Final[float] = 1.00  # < 100% ⇒ block merge & release
G6_BEHAVIOR: Final[GateBehavior] = "hard-block"

# --- G7 — Cost ceiling per suite --------------------------------------------
G7_COST_CEILING_RATIO: Final[float] = 1.20  # > 120% of declared budget
G7_BEHAVIOR: Final[GateBehavior] = "alert"

# --- G8 — Review p95 latency (production) -----------------------------------
G8_REVIEW_P95_TARGET_S: Final[float] = 60.0
G8_REVIEW_P95_CANARY_S: Final[float] = 120.0
G8_BEHAVIOR: Final[GateBehavior] = "alert"

# --- G9 — Comment SNR (production monthly audit) ----------------------------
G9_SEVERITY_MEDIUM_PLUS_MIN: Final[float] = 0.90  # < 90% of findings ≥ medium
G9_BEHAVIOR: Final[GateBehavior] = "alert"

# --- G10 — Cost per fix task (weekly trend) ---------------------------------
G10_FIX_COST_USD_MAX: Final[float] = 2.00  # mean fix cost > $2.00 ⇒ alert
G10_BEHAVIOR: Final[GateBehavior] = "alert"
