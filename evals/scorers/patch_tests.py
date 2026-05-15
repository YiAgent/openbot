"""SWE-bench-aligned patch grading — PRD §4.3 强制.

Pure scoring functions for an applied-patch test outcome. Two consumers:

  1. `evals/tasks/swe_bench_lite.py` (E2-T11) — wraps the upstream
     `inspect_evals.swe_bench` task and stamps every sample's score with
     this module's `PatchScore`.
  2. `evals/solvers/openbot_fix.py` (E2-T07) — its eval-time `@scorer`
     will call `score_patch` against the sample's `FAIL_TO_PASS` /
     `PASS_TO_PASS` lists and the test runner's per-test report.

The grading rule mirrors the upstream SWE-bench harness verbatim
(`swebench.harness.test_spec.eval_script` / `swebench.metrics`):
a sample is **resolved** iff every `FAIL_TO_PASS` test now PASSes and
every `PASS_TO_PASS` test still PASSes. Any deviation = unresolved.

Inputs are deliberately framework-free dicts so this module can score
test results from any sandbox (Modal / Docker / local) — that's what
keeps it usable before E2-T07's solver lands.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

TestStatus = Literal["PASSED", "FAILED", "ERROR", "SKIPPED"]


@dataclass(frozen=True)
class PatchScore:
    """Per-sample grading outcome — JSON-serializable via __dict__."""

    resolved: bool
    fail_to_pass_total: int
    fail_to_pass_passed: int
    pass_to_pass_total: int
    pass_to_pass_passed: int
    missing_tests: tuple[str, ...]  # tests declared by the spec but absent in results
    unexpected_failures: tuple[str, ...]  # tests that should have passed but didn't

    @property
    def fail_to_pass_resolved_ratio(self) -> float:
        if self.fail_to_pass_total == 0:
            return 1.0
        return self.fail_to_pass_passed / self.fail_to_pass_total

    @property
    def pass_to_pass_preserved_ratio(self) -> float:
        if self.pass_to_pass_total == 0:
            return 1.0
        return self.pass_to_pass_passed / self.pass_to_pass_total

    def to_dict(self) -> dict[str, object]:
        return {
            "resolved": self.resolved,
            "fail_to_pass_total": self.fail_to_pass_total,
            "fail_to_pass_passed": self.fail_to_pass_passed,
            "fail_to_pass_resolved_ratio": self.fail_to_pass_resolved_ratio,
            "pass_to_pass_total": self.pass_to_pass_total,
            "pass_to_pass_passed": self.pass_to_pass_passed,
            "pass_to_pass_preserved_ratio": self.pass_to_pass_preserved_ratio,
            "missing_tests": list(self.missing_tests),
            "unexpected_failures": list(self.unexpected_failures),
        }


def score_patch(
    *,
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    test_results: Mapping[str, TestStatus],
) -> PatchScore:
    """Grade a patch against the sample's spec.

    Args:
        fail_to_pass: test IDs that fail on the base commit and must pass
            after the patch is applied. SWE-bench schema, verbatim.
        pass_to_pass: test IDs that already pass on the base commit and
            must still pass after the patch (no regressions).
        test_results: mapping from test ID → outcome from the test runner.
            Missing keys count as failures (the test never ran).

    Returns:
        ``PatchScore``. ``resolved`` is the SWE-bench resolved boolean —
        true iff every required test outcome matches the spec.
    """
    missing: list[str] = []
    unexpected: list[str] = []

    ftp_passed = 0
    for tid in fail_to_pass:
        outcome = test_results.get(tid)
        if outcome is None:
            missing.append(tid)
            unexpected.append(tid)
            continue
        if outcome == "PASSED":
            ftp_passed += 1
        else:
            unexpected.append(tid)

    ptp_passed = 0
    for tid in pass_to_pass:
        outcome = test_results.get(tid)
        if outcome is None:
            missing.append(tid)
            unexpected.append(tid)
            continue
        if outcome == "PASSED":
            ptp_passed += 1
        else:
            unexpected.append(tid)

    resolved = ftp_passed == len(fail_to_pass) and ptp_passed == len(pass_to_pass) and not missing
    return PatchScore(
        resolved=resolved,
        fail_to_pass_total=len(fail_to_pass),
        fail_to_pass_passed=ftp_passed,
        pass_to_pass_total=len(pass_to_pass),
        pass_to_pass_passed=ptp_passed,
        missing_tests=tuple(missing),
        unexpected_failures=tuple(unexpected),
    )


def aggregate_resolved_rate(scores: list[PatchScore]) -> float:
    """Compute the suite-level resolved % — the SWE-bench leaderboard metric."""
    if not scores:
        return 0.0
    return sum(1 for s in scores if s.resolved) / len(scores)
