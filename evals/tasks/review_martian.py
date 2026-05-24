"""Martian-style PR review task — PRD §4.1 / §14 E2.

Dataset lives in **LangSmith** (published by
``python -m evals.data refresh review``). There is no local JSONL.

Trace routing to the eval project (``LANGSMITH_PROJECT_EVAL``) happens at
``evals.runtime`` package import. LangChain auto-uploads per-sample traces,
and the ``langsmith_hook.py`` anchors each sample to a LangSmith Experiment
Run automatically from ``Task.metadata``.

Run::

    doppler run --project openbot --config dev -- \\
        uv run inspect eval \\
        'evals/tasks/review_martian.py@review_martian_openbot'

For a cheap smoke add ``--limit 5``.
"""

from __future__ import annotations

from inspect_ai import Task, task

from evals.data import REVIEW
from evals.scorers.review_judge import MARTIAN_JUDGE_MODEL_ID, MARTIAN_JUDGE_VERSION
from evals.solvers.review import openbot_review_solver
from evals.tasks._review_overlap_scorer import make_review_overlap_scorer


@task
def review_martian_openbot() -> Task:
    """OpenBot review solver + verbatim Martian-CRB LLM judge.

    Pairs the OpenBot review solver with the judge surface from
    ``withmartian/code-review-benchmark`` (model ``claude-opus-4-5``,
    temperature 0, max_tokens 512, prompt body byte-identical to martian's
    ``step3_judge_comments.py``).
    """
    return REVIEW.build_task(
        solver=openbot_review_solver(),
        scorer=make_review_overlap_scorer(),
        extra_metadata={
            "judge_label": "martian_crb_verbatim",
            "judge_model_id": MARTIAN_JUDGE_MODEL_ID,
            "judge_prompt_version": MARTIAN_JUDGE_VERSION,
        },
    )
