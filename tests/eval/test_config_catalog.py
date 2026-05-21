"""Contracts for centralised eval catalog configuration."""

from __future__ import annotations

import re
from pathlib import Path

from evals.common.config import get_eval_config


def test_eval_config_owns_dataset_catalog() -> None:
    catalog = get_eval_config().catalog

    assert catalog.solver_family_baseline == "deepagents_baseline"
    assert catalog.review.dataset_version == "martian_2026w20"
    assert catalog.chat.dataset_version == "chat_swe_qa_pro_v1"
    assert catalog.chat.hf_dataset == "TIGER-Lab/SWE-QA-Pro-Bench"
    assert catalog.fix.dataset_version == "fix_swe_bench_verified"
    assert catalog.fix.hf_dataset == "princeton-nlp/SWE-bench_Verified"
    assert catalog.swt.dataset_version == "test_swt_bench_verified"
    assert catalog.swt.hf_dataset == "eth-sri/SWT-bench_Verified_bm25_27k_zsb"
    assert catalog.fix.dataset_source == "huggingface:princeton-nlp/SWE-bench_Verified"
    assert (
        catalog.chat.dataset_source
        == "langsmith:chat_swe_qa_pro_v1 (mirror of huggingface:TIGER-Lab/SWE-QA-Pro-Bench)"
    )
    assert catalog.swe_export_feedback_key == "swe_export_ok"
    assert catalog.swt_export_feedback_key == "swt_export_ok"
    assert catalog.swe_harness_feedback_key == "swe_bench_pass_at_1"
    assert catalog.swt_harness_feedback_key == "swt_bench_pass_at_1"
    assert catalog.unit_feedback_config == {"type": "continuous", "min": 0.0, "max": 1.0}


def test_task_and_dataset_scripts_do_not_redeclare_catalog_constants() -> None:
    forbidden_assignments = (
        "_DATASET_VERSION =",
        "_HF_DATASET =",
        "_DATASET_SOURCE =",
        "DATASET_NAME =",
        "HF_DATASET =",
        "HF_REPO_ID =",
    )
    # Use an absolute path anchored to the project root so this test is
    # unaffected by the autouse _isolate_openbot_env fixture that chdirs
    # to a temporary directory before every test.
    _project_root = Path(__file__).parent.parent.parent
    checked_paths = [
        *(_project_root / "evals/tasks").glob("*.py"),
        *(_project_root / "evals/scripts").glob("build_*_dataset.py"),
    ]

    for path in checked_paths:
        source = path.read_text(encoding="utf-8")
        for needle in forbidden_assignments:
            name = needle.replace(" =", "")
            assert not re.search(rf"^{name}\s*=", source, re.MULTILINE), (
                f"{path} should import catalog config, not declare {name}"
            )
