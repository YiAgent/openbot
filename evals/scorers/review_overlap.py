"""Review overlap scorer — judge-based precision / recall / F1.

PRD §4.1 + §6.1. Compares candidate review findings against golden ones via
an LLM judge; produces an overlap report plus the unmatched sets on both
sides (needed for failure-category attribution in PRD §10.2 / §12.4).

Two layers:

- :func:`compute_review_overlap` — pure-function core. Takes an injected
  judge callable so tests can run without a live LLM.
- :func:`review_overlap_f1_scorer` — Inspect AI ``@scorer`` factory. Reads
  ``state.metadata["candidate_findings"]`` (populated by the review solver)
  and ``target.text`` (golden findings as JSON), runs the overlap math, and
  returns a normalized ``Score`` with the precision / recall / matched /
  unmatched breakdown in ``metadata`` for downstream attribution.

Tasks reference the factory; they never define a scorer inline. This mirrors
the contract documented in :mod:`evals.scorers.swe_qa_pro`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypedDict

from inspect_ai.scorer import Score, Target, mean, scorer, stderr
from inspect_ai.solver import TaskState


class Finding(TypedDict):
    """Output shape from `evals.solvers.review` (PRD §4.1)."""

    file: str
    line: int | None
    body: str
    severity: str  # "low" | "medium" | "high"


class JudgeVerdict(TypedDict):
    """Shape returned by the judge LLM (PRD §10.3)."""

    match: bool
    confidence: float
    rationale: str


JudgeFn = Callable[[Finding, Finding], JudgeVerdict]


@dataclass(frozen=True)
class OverlapReport:
    """Result of comparing candidate findings against golden findings."""

    precision: float
    recall: float
    f1: float
    matched_pairs: list[tuple[int, int]] = field(default_factory=list)
    """`(golden_idx, candidate_idx)` pairs the judge accepted."""
    unmatched_golden: list[int] = field(default_factory=list)
    """Indices of golden findings the candidate failed to cover."""
    unmatched_candidate: list[int] = field(default_factory=list)
    """Indices of candidate findings that don't match any golden (false positives)."""


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def compute_review_overlap(
    golden: list[Finding],
    candidate: list[Finding],
    judge: JudgeFn,
) -> OverlapReport:
    """Greedy one-to-one match candidate→golden by judge verdict; report metrics.

    Algorithm:
      1. For each candidate, iterate over unmatched golden findings; ask judge.
      2. First True verdict consumes that golden + candidate pair.
      3. Remaining candidates are false positives; remaining golden are misses.

    Edge cases (sklearn's ``zero_division=0`` convention, with one carve-out):
      - Both empty (golden ∅, candidate ∅): precision = recall = f1 = 1.0.
        Vacuous perfect — the model correctly predicted "no findings" on a PR
        that genuinely has none. Punishing this case with 0 would tank
        aggregate scores on clean-PR-heavy datasets where the right answer is
        "nothing to flag", which is the common case in review eval.
      - Empty candidate, non-empty golden (TP=FP=0): precision = 0, recall = 0,
        f1 = 0. The model contributed no signal, so the metric should reflect
        that — not the legacy ``precision = 1.0`` which silently inflated
        dataset-level averages whenever the agent truncated mid-thought and
        emitted nothing. This is the bug the eval-trap fix is here for.
      - Non-empty candidate, empty golden (TP=FN=0): precision = 0, recall = 0,
        f1 = 0. The model invented findings on a clean PR; same rationale —
        ``recall = 1.0`` vacuous would reward over-flagging behaviour.
    """
    if not golden and not candidate:
        return OverlapReport(precision=1.0, recall=1.0, f1=1.0)

    consumed_golden: set[int] = set()
    consumed_candidate: set[int] = set()
    matched_pairs: list[tuple[int, int]] = []

    for c_idx, c_finding in enumerate(candidate):
        for g_idx, g_finding in enumerate(golden):
            if g_idx in consumed_golden:
                continue
            verdict = judge(g_finding, c_finding)
            if verdict["match"]:
                consumed_golden.add(g_idx)
                consumed_candidate.add(c_idx)
                matched_pairs.append((g_idx, c_idx))
                break

    tp = len(matched_pairs)
    # zero_division=0: precision/recall are 0 (not 1) when the denominator is
    # 0. The both-empty short-circuit above is the only vacuous-perfect case.
    precision = (tp / len(candidate)) if candidate else 0.0
    recall = (tp / len(golden)) if golden else 0.0

    return OverlapReport(
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
        matched_pairs=matched_pairs,
        unmatched_golden=[i for i in range(len(golden)) if i not in consumed_golden],
        unmatched_candidate=[i for i in range(len(candidate)) if i not in consumed_candidate],
    )


def review_overlap_f1_scorer(judge: JudgeFn):  # type: ignore[no-untyped-def]
    """Inspect AI scorer factory — overlap F1 against golden findings.

    Inputs (from the solver / dataset):
      - ``state.metadata["candidate_findings"]`` — ``list[Finding]`` produced
        by :mod:`evals.solvers.review`. Empty list if the solver emitted no
        findings.
      - ``target.text`` — golden findings as a JSON string. Decoded with
        ``json.loads``; on parse error the golden set is treated as empty
        (matches the legacy behaviour expected by the dataset shape).

    Output: a :class:`Score` whose ``value`` is the F1, with precision /
    recall / matched_pairs / unmatched_* / counts in ``metadata`` for
    PRD §10.2 / §12.4 attribution.
    """

    @scorer(metrics=[mean(), stderr()])
    def _factory():  # type: ignore[no-untyped-def]
        async def _score(state: TaskState, target: Target) -> Score:
            candidate: list[Finding] = state.metadata.get("candidate_findings", [])
            try:
                golden: list[Finding] = json.loads(target.text)
            except (json.JSONDecodeError, TypeError):
                golden = []

            report = compute_review_overlap(golden, candidate, judge)
            return Score(
                value=report.f1,
                answer=json.dumps({"findings": candidate}, ensure_ascii=False),
                explanation=(
                    f"precision={report.precision:.3f} recall={report.recall:.3f} "
                    f"f1={report.f1:.3f} matched={len(report.matched_pairs)} "
                    f"unmatched_golden={len(report.unmatched_golden)} "
                    f"unmatched_candidate={len(report.unmatched_candidate)}"
                ),
                metadata={
                    "precision": report.precision,
                    "recall": report.recall,
                    "f1": report.f1,
                    "matched_pairs": report.matched_pairs,
                    "unmatched_golden": report.unmatched_golden,
                    "unmatched_candidate": report.unmatched_candidate,
                    "candidate_count": len(candidate),
                    "golden_count": len(golden),
                },
            )

        return _score

    return _factory()
