"""Review overlap scorer — judge-based precision / recall / F1.

PRD §4.1 + §6.1. Compares candidate review findings against golden ones via
an LLM judge; produces an overlap report plus the unmatched sets on both
sides (needed for failure-category attribution in PRD §10.2 / §12.4).

Pure-function core (`compute_review_overlap`) takes an injected judge callable
so tests can avoid live LLM calls. The inspect-ai `@scorer` shim sits in
`review_overlap_scorer()` for v0.1; it's wired up once E1-T06 review solver
gets real `TaskState.output` shape, but the math is identical.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypedDict


class Finding(TypedDict):
    """Output shape from `evals.solvers.openbot_review` (PRD §4.1)."""

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

    Edge cases:
      - Empty golden: precision = (1.0 if candidate is empty else 0.0), recall = 1.0.
      - Empty candidate: precision = 1.0, recall = (1.0 if golden empty else 0.0).
      - Both empty: precision = recall = f1 = 1.0 (vacuously perfect).
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
    precision = tp / len(candidate) if candidate else 1.0
    recall = tp / len(golden) if golden else 1.0

    return OverlapReport(
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
        matched_pairs=matched_pairs,
        unmatched_golden=[i for i in range(len(golden)) if i not in consumed_golden],
        unmatched_candidate=[i for i in range(len(candidate)) if i not in consumed_candidate],
    )


def review_overlap_scorer():  # type: ignore[no-untyped-def]
    """Inspect AI `@scorer` shim — wraps `compute_review_overlap` for runner.

    Not callable until E1-T06 review solver lands a concrete `TaskState.output`
    shape: we don't yet know the inspect-ai version's exact `Score` ctor args.
    Raising on construction keeps misuse loud.
    """
    raise NotImplementedError(
        "review_overlap_scorer() requires E1-T06 review solver output shape. "
        "Use evals.scorers.review_overlap.compute_review_overlap directly until then."
    )
