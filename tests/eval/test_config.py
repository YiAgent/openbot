"""Budget constants must stay 1:1 with eval PRD §11.1 — break-on-drift tests.

These assertions are intentionally hard-coded to PRD §11.1 numbers. If a value
changes here without a matching PRD § + `docs/eval/baseline-log.md` entry, the
test fails. That is by design (governance: PRD §11 cost guardrails).
"""

from __future__ import annotations

import pytest

from evals.common import config


def test_per_sample_usd_matches_prd() -> None:
    # eval PRD §11.1 verbatim
    assert dict(config.PER_SAMPLE_USD) == {
        "review_shadow": 0.50,
        "review_martian": 0.60,
        "triage_internal": 0.20,
        "fix_internal_smoke": 3.00,
        "swe_bench_lite": 1.50,
        "redteam_prompt_injection": 0.05,
    }


def test_per_suite_run_usd_matches_prd() -> None:
    # eval PRD §11.1 verbatim
    assert dict(config.PER_SUITE_RUN_USD) == {
        "smoke": 5,
        "regression": 30,
        "weekly": 200,
        "release": 800,
    }


def test_monthly_total_usd_matches_prd() -> None:
    # eval PRD §11.1 verbatim
    assert config.MONTHLY_TOTAL_USD == 1500


@pytest.mark.parametrize("name", ["PER_SAMPLE_USD", "PER_SUITE_RUN_USD"])
def test_budget_mappings_are_immutable(name: str) -> None:
    """Maps go through MappingProxyType so accidental in-place mutation fails fast."""
    mapping = getattr(config, name)
    with pytest.raises(TypeError):
        mapping["__attempt_to_mutate__"] = 999  # type: ignore[index]
