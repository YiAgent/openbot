"""Scorer factory for Martian-CRB review overlap evaluation.

Extracted from review_martian.py to keep scorer wiring out of the task body.
"""

from __future__ import annotations

from typing import cast

from evals.scorers.review_judge import judge_verdict as martian_judge_verdict
from evals.scorers.review_overlap import JudgeFn, review_overlap_f1_scorer


def make_review_overlap_scorer() -> object:
    """Return the Martian-verbatim review overlap F1 scorer."""
    return review_overlap_f1_scorer(cast(JudgeFn, martian_judge_verdict))
