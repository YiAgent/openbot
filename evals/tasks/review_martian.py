"""Martian-style PR review task — PRD §4.1 / §14 E2.

Dataset lives in **LangSmith** (published by
``evals/scripts/build_review_martian_dataset.py``); this task pulls Examples via
``evals.common.datasets.langsmith_dataset``. There is no local JSONL — routing
between the public / internal LangSmith projects is driven by the allowlist
in ``evals.common.langsmith`` (``configure_tracing_for_dataset``).

Run::

    doppler run --project openbot --config dev -- \\
        uv run inspect eval \\
        'evals/tasks/review_martian.py@review_martian_baseline_crb'

For a cheap smoke add ``--limit 5``.
"""

from __future__ import annotations

import json
import re as _re
from collections.abc import Callable

from inspect_ai import Task, task
from inspect_ai.scorer import Score, Target, mean, scorer, stderr
from inspect_ai.solver import TaskState

from evals.common.datasets import langsmith_dataset
from evals.common.langsmith import configure_tracing_for_dataset
from evals.scorers.review_judge import (
    MARTIAN_JUDGE_MODEL_ID,
    MARTIAN_JUDGE_VERSION,
)
from evals.scorers.review_judge import (
    judge_verdict as martian_judge_verdict,
)
from evals.scorers.review_overlap import Finding, JudgeVerdict, compute_review_overlap
from evals.solvers.registry import get_review_solver

_DATASET_VERSION = "martian_2026w20"

JudgeFn = Callable[[Finding, Finding], JudgeVerdict]


def _heuristic_judge(g: Finding, c: Finding) -> JudgeVerdict:
    """Deterministic, no-LLM judge.

    Kept as a free / token-less alternative to the Martian-CRB LLM judge for
    local development. The real apples-to-apples baseline uses
    ``martian_judge_verdict``.
    """
    if g["file"] and g["file"] != c["file"]:
        return {"match": False, "confidence": 0.0, "rationale": "file differs"}

    gl, cl = g.get("line"), c.get("line")
    if gl is not None and cl is not None and abs(gl - cl) > 3:
        return {"match": False, "confidence": 0.0, "rationale": "line distance > 3"}

    def toks(s: str) -> set[str]:
        return set(_re.findall(r"[a-zA-Z]{4,}", s.lower()))

    shared = toks(g["body"]) & toks(c["body"])
    if not shared:
        return {"match": False, "confidence": 0.1, "rationale": "no keyword overlap"}
    return {"match": True, "confidence": 0.8, "rationale": f"overlap on {sorted(shared)[:3]}"}


def _build_overlap_scorer(judge: JudgeFn):  # type: ignore[no-untyped-def]
    """Construct an Inspect AI ``@scorer`` bound to a specific judge fn."""

    @scorer(metrics=[mean(), stderr()])
    def _scorer():  # type: ignore[no-untyped-def]
        async def _score(state: TaskState, target: Target) -> Score:
            candidate: list[Finding] = state.metadata.get("candidate_findings", [])
            golden_raw = target.text
            try:
                golden: list[Finding] = json.loads(golden_raw)
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

    return _scorer()


def _build_task(
    *,
    solver_id: str,
    judge: JudgeFn,
    judge_label: str,
    judge_model_id: str | None,
    judge_prompt_version: int | None,
) -> Task:
    """Shared task constructor — only solver / judge differ per ``@task``."""
    # PRD §13.2: route this run's traces to the public / internal LangSmith
    # project based on the dataset manifest. Also a no-op when the LangSmith
    # API key is absent.
    configure_tracing_for_dataset(_DATASET_VERSION)

    solver_family = "baseline" if solver_id == "deepagents_baseline" else "production"
    solver_factory = get_review_solver(solver_id)
    return Task(
        dataset=langsmith_dataset(_DATASET_VERSION),
        solver=solver_factory(),
        scorer=_build_overlap_scorer(judge),
        metadata={
            "dataset_version": _DATASET_VERSION,
            "solver_id": solver_id,
            "solver_family": solver_family,
            "judge_label": judge_label,
            "judge_model_id": judge_model_id,
            "judge_prompt_version": judge_prompt_version,
        },
    )


@task
def review_martian_baseline_crb() -> Task:
    """Deepagents baseline + verbatim Martian-CRB LLM judge.

    Pair the durable ``deepagents_baseline`` review solver with the judge
    surface from ``withmartian/code-review-benchmark`` (model
    ``claude-opus-4-5``, temperature 0, max_tokens 512, prompt body byte-
    identical to martian's ``step3_judge_comments.py``). Same judge powers
    open-swe's reviewer baseline, so micro/macro P/R/F1 numbers are
    directly comparable across the two projects.
    """
    return _build_task(
        solver_id="deepagents_baseline",
        judge=martian_judge_verdict,
        judge_label="martian_crb_verbatim",
        judge_model_id=MARTIAN_JUDGE_MODEL_ID,
        judge_prompt_version=MARTIAN_JUDGE_VERSION,
    )


@task
def review_martian_baseline_heuristic() -> Task:
    """Same dataset + solver but a zero-cost heuristic judge.

    Use for cheap iteration / CI smoke. Numbers are not comparable to martian
    or open-swe — the heuristic systematically under-counts (martian gold
    comments carry no file/line, so the file-equality and line-distance gates
    fall through to keyword overlap only).
    """
    return _build_task(
        solver_id="deepagents_baseline",
        judge=_heuristic_judge,
        judge_label="heuristic",
        judge_model_id=None,
        judge_prompt_version=None,
    )


@task
def review_martian_openbot() -> Task:
    """Future production OpenBot provider on the same dataset / judge.

    Reserved for when ``openbot.workflows.review.run(...)`` ships; today
    ``get_review_solver('openbot_prod')`` raises NotImplementedError so
    importing this task is safe but invoking it fails fast.
    """
    return _build_task(
        solver_id="openbot_prod",
        judge=martian_judge_verdict,
        judge_label="martian_crb_verbatim",
        judge_model_id=MARTIAN_JUDGE_MODEL_ID,
        judge_prompt_version=MARTIAN_JUDGE_VERSION,
    )
