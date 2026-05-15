"""E2-T08 — pin SWE-bench-aligned patch grading.

Test fixtures use the documented SWE-bench resolved rule: a sample is
resolved iff every FAIL_TO_PASS test passes AND every PASS_TO_PASS test
still passes. Anything else is unresolved. These tests fail loudly if the
scorer drifts from upstream semantics.
"""

from __future__ import annotations

from evals.scorers.patch_tests import (
    PatchScore,
    aggregate_resolved_rate,
    score_patch,
)


def test_resolved_when_all_ftp_pass_and_all_ptp_preserved() -> None:
    s = score_patch(
        fail_to_pass=["test_bug_a", "test_bug_b"],
        pass_to_pass=["test_existing_1"],
        test_results={
            "test_bug_a": "PASSED",
            "test_bug_b": "PASSED",
            "test_existing_1": "PASSED",
        },
    )
    assert s.resolved is True
    assert s.fail_to_pass_passed == 2
    assert s.pass_to_pass_passed == 1
    assert s.fail_to_pass_resolved_ratio == 1.0
    assert s.pass_to_pass_preserved_ratio == 1.0


def test_unresolved_when_one_ftp_still_fails() -> None:
    s = score_patch(
        fail_to_pass=["test_bug_a", "test_bug_b"],
        pass_to_pass=["test_existing_1"],
        test_results={
            "test_bug_a": "PASSED",
            "test_bug_b": "FAILED",
            "test_existing_1": "PASSED",
        },
    )
    assert s.resolved is False
    assert s.fail_to_pass_passed == 1
    assert "test_bug_b" in s.unexpected_failures


def test_unresolved_when_ptp_regresses() -> None:
    """A patch that fixes the bug but breaks a passing test must not resolve."""
    s = score_patch(
        fail_to_pass=["test_bug_a"],
        pass_to_pass=["test_existing_1", "test_existing_2"],
        test_results={
            "test_bug_a": "PASSED",
            "test_existing_1": "PASSED",
            "test_existing_2": "FAILED",
        },
    )
    assert s.resolved is False
    assert "test_existing_2" in s.unexpected_failures


def test_missing_test_counts_as_unexpected_failure() -> None:
    """A test that never ran is treated as a failure (cannot prove it passed)."""
    s = score_patch(
        fail_to_pass=["test_bug_a"],
        pass_to_pass=["test_existing_1"],
        test_results={
            "test_bug_a": "PASSED",
            # test_existing_1 missing → never ran
        },
    )
    assert s.resolved is False
    assert "test_existing_1" in s.missing_tests
    assert "test_existing_1" in s.unexpected_failures


def test_error_status_counts_as_unexpected_failure() -> None:
    s = score_patch(
        fail_to_pass=["test_bug_a"],
        pass_to_pass=[],
        test_results={"test_bug_a": "ERROR"},
    )
    assert s.resolved is False
    assert "test_bug_a" in s.unexpected_failures


def test_skipped_test_counts_as_unexpected_failure() -> None:
    """SWE-bench grading does not credit skipped tests as passes."""
    s = score_patch(
        fail_to_pass=["test_bug_a"],
        pass_to_pass=[],
        test_results={"test_bug_a": "SKIPPED"},
    )
    assert s.resolved is False


def test_empty_spec_is_trivially_resolved() -> None:
    """A spec with no required tests is vacuously resolved (defensive — SWE-bench samples never look like this)."""
    s = score_patch(fail_to_pass=[], pass_to_pass=[], test_results={})
    assert s.resolved is True
    assert s.fail_to_pass_resolved_ratio == 1.0
    assert s.pass_to_pass_preserved_ratio == 1.0


def test_to_dict_is_json_safe() -> None:
    import json

    s = score_patch(
        fail_to_pass=["t1"],
        pass_to_pass=["t2"],
        test_results={"t1": "PASSED", "t2": "PASSED"},
    )
    serialized = json.dumps(s.to_dict())
    parsed = json.loads(serialized)
    assert parsed["resolved"] is True
    assert parsed["fail_to_pass_total"] == 1
    assert parsed["pass_to_pass_preserved_ratio"] == 1.0


def test_aggregate_resolved_rate() -> None:
    scores = [
        PatchScore(True, 1, 1, 1, 1, (), ()),
        PatchScore(False, 1, 0, 1, 1, (), ("t1",)),
        PatchScore(True, 1, 1, 1, 1, (), ()),
        PatchScore(False, 1, 1, 1, 0, (), ("t2",)),
    ]
    assert aggregate_resolved_rate(scores) == 0.5
    assert aggregate_resolved_rate([]) == 0.0


def test_score_matches_swe_bench_published_outcome_fixture() -> None:
    """Lock against an excerpt of SWE-bench's published grading.

    Sample ``django__django-11099`` (SWE-bench Lite) declares one
    FAIL_TO_PASS test and a small PASS_TO_PASS set; the upstream
    grading harness reports ``resolved: true`` when all of them pass
    after applying the gold patch. The exact test IDs below are
    paraphrased to keep this test self-contained, but the rule is the
    one SWE-bench enforces.
    """
    s = score_patch(
        fail_to_pass=[
            "test_ascii_username_validator (auth_tests.test_validators.UsernameValidatorsTests)",
        ],
        pass_to_pass=[
            "test_unicode_username_validator (auth_tests.test_validators.UsernameValidatorsTests)",
            "test_password_validators_help_text_html (auth_tests.test_validators.PasswordValidationTest)",
        ],
        test_results={
            "test_ascii_username_validator (auth_tests.test_validators.UsernameValidatorsTests)": "PASSED",
            "test_unicode_username_validator (auth_tests.test_validators.UsernameValidatorsTests)": "PASSED",
            "test_password_validators_help_text_html (auth_tests.test_validators.PasswordValidationTest)": "PASSED",
        },
    )
    assert s.resolved is True
