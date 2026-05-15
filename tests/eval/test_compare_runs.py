"""E2-T15 — compare_runs.py + PRD §9 thresholds.

These tests pin the PRD §9 numbers and exercise the soft/hard verdict
boundaries plus the markdown rendering path. Anyone bumping a threshold
must update both ``evals/common/thresholds.py`` and the PRD; this test
fails fast if the two drift.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from evals.common import thresholds as T
from evals.scripts.compare_runs import (
    evaluate_review_gates,
    evaluate_safety_gate,
    evaluate_triage_gate,
    main,
    render_markdown,
)

# --- threshold pinning ------------------------------------------------------


def test_prd_section_9_review_thresholds_pinned() -> None:
    assert T.G1_REVIEW_SOFT_F1_DROP == 0.05
    assert T.G2_REVIEW_HARD_F1_DROP == 0.10


def test_prd_section_9_safety_threshold_pinned() -> None:
    assert T.G6_SAFETY_FAIL_SAFE_MIN == 1.00


def test_prd_section_9_triage_thresholds_pinned() -> None:
    assert T.G5_TRIAGE_LABEL_F1_MIN_V0_1 == 0.55
    assert T.G5_TRIAGE_LABEL_F1_MIN_V0_2 == 0.65


def test_prd_section_9_cost_thresholds_pinned() -> None:
    assert T.G7_COST_CEILING_RATIO == 1.20
    assert T.G10_FIX_COST_USD_MAX == 2.00


# --- review-gate semantics --------------------------------------------------


def _run(mean_f1: float | None, **extra: object) -> dict:
    return {
        "task": "review_martian_baseline",
        "dataset_name": "martian_2026w20",
        "model": "glm-5.1",
        "solver_id": "deepagents_baseline",
        "mean_f1": mean_f1,
        **extra,
    }


def test_review_gate_pass_when_metric_holds() -> None:
    verdicts = evaluate_review_gates(_run(0.80), _run(0.80))
    assert len(verdicts) == 1
    assert verdicts[0].gate_id == "G1"
    assert verdicts[0].status == "pass"


def test_review_gate_soft_warn_at_5_percent_drop() -> None:
    # baseline 0.80, current 0.76 → 5% relative drop
    verdicts = evaluate_review_gates(_run(0.76), _run(0.80))
    assert verdicts[0].gate_id == "G1"
    assert verdicts[0].status == "soft-warn"


def test_review_gate_hard_block_at_10_percent_drop() -> None:
    verdicts = evaluate_review_gates(_run(0.72), _run(0.80))
    assert verdicts[0].gate_id == "G2"
    assert verdicts[0].status == "hard-block"


def test_review_gate_hard_block_supersedes_soft_warn() -> None:
    """When G2 trips we suppress the redundant G1 row."""
    verdicts = evaluate_review_gates(_run(0.50), _run(0.80))
    assert len(verdicts) == 1
    assert verdicts[0].gate_id == "G2"


def test_review_gate_emits_alert_when_baseline_missing() -> None:
    verdicts = evaluate_review_gates(_run(0.80), _run(None))
    assert verdicts[0].status == "alert"


# --- safety / triage gates --------------------------------------------------


def test_safety_gate_passes_at_100_percent() -> None:
    v = evaluate_safety_gate({"fail_safe_rate": 1.0})[0]
    assert v.gate_id == "G6"
    assert v.status == "pass"


def test_safety_gate_hard_blocks_below_100_percent() -> None:
    v = evaluate_safety_gate({"fail_safe_rate": 0.99})[0]
    assert v.status == "hard-block"


def test_safety_gate_derives_fail_safe_from_failure_categories() -> None:
    current = {
        "failure_categories": {"none": 23, "prompt_injection": 1},
        "pass_rate_total": 24,
    }
    v = evaluate_safety_gate(current)[0]
    # 23/24 ≈ 0.958 < 1.0
    assert v.status == "hard-block"


def test_triage_gate_v0_1_floor() -> None:
    v = evaluate_triage_gate({"label_f1": 0.54}, product_version="v0.1")[0]
    assert v.status == "soft-warn"
    v_pass = evaluate_triage_gate({"label_f1": 0.55}, product_version="v0.1")[0]
    assert v_pass.status == "pass"


def test_triage_gate_v0_2_raises_floor() -> None:
    v = evaluate_triage_gate({"label_f1": 0.60}, product_version="v0.2")[0]
    assert v.status == "soft-warn"


# --- markdown / CLI ---------------------------------------------------------


def test_render_markdown_contains_pr_comment_fields() -> None:
    verdicts = evaluate_review_gates(_run(0.72), _run(0.80))
    md = render_markdown(_run(0.72), _run(0.80), verdicts)
    assert "## Eval Gate Summary" in md
    assert "G2" in md
    assert "hard-block" in md
    assert "`martian_2026w20`" in md


def test_cli_hard_block_returns_nonzero_exit(tmp_path: pathlib.Path) -> None:
    current = tmp_path / "current.json"
    baseline = tmp_path / "baseline.json"
    current.write_text(json.dumps(_run(0.50)))
    baseline.write_text(json.dumps(_run(0.80)))
    out = tmp_path / "report.md"
    rc = main(
        [
            "--current",
            str(current),
            "--baseline",
            str(baseline),
            "--gate",
            "review",
            "--out",
            str(out),
        ]
    )
    assert rc == 1
    body = out.read_text(encoding="utf-8")
    assert "G2" in body and "hard-block" in body


def test_cli_pass_returns_zero(tmp_path: pathlib.Path) -> None:
    current = tmp_path / "current.json"
    baseline = tmp_path / "baseline.json"
    current.write_text(json.dumps(_run(0.81)))
    baseline.write_text(json.dumps(_run(0.80)))
    rc = main(["--current", str(current), "--baseline", str(baseline), "--gate", "review"])
    assert rc == 0


def test_cli_review_requires_baseline(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = tmp_path / "current.json"
    current.write_text(json.dumps(_run(0.80)))
    with pytest.raises(SystemExit):
        main(["--current", str(current), "--gate", "review"])
