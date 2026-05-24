"""Martian-style PR review task — PRD §4.1 / §14 E2.

Dataset lives in **LangSmith** (published by
``evals/scripts/build_review_martian_dataset.py``); this task pulls Examples via
``evals.common.datasets.langsmith_dataset``. There is no local JSONL — routing
between the public / internal LangSmith projects is driven by the allowlist
in ``evals.inspect.langsmith`` (``configure_tracing_for_dataset``).

Run::

    doppler run --project openbot --config dev -- \\
        uv run inspect eval \\
        'evals/tasks/review_martian.py@review_martian_baseline_crb'

For a cheap smoke add ``--limit 5``.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from inspect_ai import Task, task
from inspect_ai.scorer import Score, Target, mean, scorer, stderr
from inspect_ai.solver import Solver, TaskState

from evals.common.config import get_eval_config
from evals.common.datasets import langsmith_dataset
from evals.inspect.langsmith import LangSmithExperiment, configure_tracing_for_dataset
from evals.scorers.review_judge import (
    MARTIAN_JUDGE_MODEL_ID,
    MARTIAN_JUDGE_VERSION,
)
from evals.scorers.review_judge import (
    judge_verdict as martian_judge_verdict,
)
from evals.scorers.review_overlap import Finding, JudgeVerdict, compute_review_overlap
from evals.solvers.review import openbot_review_solver

JudgeFn = Callable[[Finding, Finding], JudgeVerdict]


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
    solver: Solver,
    solver_id: str,
    judge: JudgeFn,
    judge_label: str,
    judge_model_id: str | None,
    judge_prompt_version: int | None,
) -> Task:
    """Shared task constructor — only solver / judge differ per ``@task``."""
    catalog = get_eval_config().catalog
    dataset_version = catalog.review.dataset_version

    # PRD §13.2: route this run's traces to the public / internal LangSmith
    # project based on the dataset manifest. Also a no-op when the LangSmith
    # API key is absent.
    configure_tracing_for_dataset(dataset_version)

    # solver_family is the LangSmith Experiment grouping key — keep consistent
    # across all v0.1 task agents so the Experiments tab stays comparable.
    solver_family = solver_id

    # Surface this run as a LangSmith Experiment so review F1 shows up on the
    # Experiments tab next to fix/test cells. ``instance_id_field="id"`` maps
    # ``state.sample_id`` back to the LangSmith Example via ``inputs.id``
    # (see evals/scripts/build_review_martian_dataset.py).
    experiment = LangSmithExperiment.start(
        dataset_name=dataset_version,
        solver_family=solver_family,
        instance_id_field="id",
    )

    return Task(
        dataset=langsmith_dataset(dataset_version),
        solver=solver,
        scorer=experiment.wrap(
            _build_overlap_scorer(judge),
            metrics=[mean(), stderr()],
            scorer_name="review_overlap_f1",
            feedback_key="review_overlap_f1",
            feedback_config=catalog.unit_feedback_config,
        ),
        # No task-level sandbox: review is closed-form over the diff in
        # ``state.input_text``. Fix/test tasks use a Daytona sandbox at the
        # solver layer; review doesn't need repo access because the diff IS
        # the input.
        metadata={
            "dataset_version": dataset_version,
            "solver_id": solver_id,
            "solver_family": solver_family,
            "judge_label": judge_label,
            "judge_model_id": judge_model_id,
            "judge_prompt_version": judge_prompt_version,
            **experiment.metadata(),
        },
    )


@task
def review_martian_baseline_crb() -> Task:
    """OpenBot review solver + verbatim Martian-CRB LLM judge.

    Pairs the OpenBot review solver with the judge surface from
    ``withmartian/code-review-benchmark`` (model ``claude-opus-4-5``,
    temperature 0, max_tokens 512, prompt body byte-identical to martian's
    ``step3_judge_comments.py``). Same judge powers open-swe's reviewer
    baseline, so micro/macro P/R/F1 numbers are directly comparable across
    the two projects.
    """
    return _build_task(
        solver=openbot_review_solver(),
        solver_id="openbot_agent",
        judge=martian_judge_verdict,
        judge_label="martian_crb_verbatim",
        judge_model_id=MARTIAN_JUDGE_MODEL_ID,
        judge_prompt_version=MARTIAN_JUDGE_VERSION,
    )


@task
def review_martian_openbot() -> Task:
    """Future production OpenBot provider on the same dataset / judge.

    Reserved for when ``openbot.application.workflows.review.run(...)`` ships; today
    invoking this task raises immediately. The entry exists so the eval
    surface is wired and only a solver swap is needed once the workflow
    lands (mirrors :func:`evals.tasks.chat_swe_qa_pro.chat_swe_qa_pro_openbot`).
    """
    raise NotImplementedError(
        "Review solver provider 'openbot_prod' is reserved until "
        "openbot.application.workflows.review.run(...) ships."
    )
