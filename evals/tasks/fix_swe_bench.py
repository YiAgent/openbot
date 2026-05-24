"""SWE-bench Verified — PRD §3.1 (``fix_swe_bench`` eval surface).

Architecture:

- Solver runs the OpenBot fix workflow in a Daytona sandbox cloned at
  ``base_commit`` (see :mod:`evals.solvers.fix`).
- The "scorer" is :func:`evals.runtime.prediction_export.prediction_exporter`,
  which validates the agent's :class:`SweBenchPrediction` against the
  official schema and appends it to
  ``evals/outputs/fix_swe_bench/<run-label>.predictions.jsonl``.
- **Actual grading happens offline** via the SWE-bench Docker harness:

    python -m swebench.harness.run_evaluation \\
        --dataset_name princeton-nlp/SWE-bench_Verified \\
        --predictions_path evals/outputs/fix_swe_bench/<run>.predictions.jsonl \\
        --max_workers 8 --run_id openbot-{date}

Run::

    doppler run --project openbot --config dev -- \\
        uv run inspect eval 'evals/tasks/fix_swe_bench.py' --limit 5
"""

from __future__ import annotations

from inspect_ai import Task, task

from evals.runtime.config import get_eval_config
from evals.runtime.environment import build_export_experiment, git_sha, resolve_model_label
from evals.runtime.hf_datasets import load_issue_dataset
from evals.runtime.langsmith import configure_tracing_for_dataset
from evals.runtime.predictions import SweBenchPrediction
from evals.solvers.fix import openbot_fix_solver


@task
def fix_swe_bench() -> Task:
    """SWE-bench Verified with the OpenBot fix solver.

    Produces ``predictions.jsonl`` in the official SWE-bench format.
    Real grading is offline via the SWE-bench Docker harness.
    """
    catalog = get_eval_config().catalog
    dataset_version = catalog.fix.dataset_version
    configure_tracing_for_dataset(dataset_version)

    sha = git_sha()
    model_label = resolve_model_label()

    exp = build_export_experiment(
        dataset_version=dataset_version,
        solver_family=catalog.solver_family_baseline,
        model=model_label,
        git_sha=sha,
        schema=SweBenchPrediction,
        scorer_name=catalog.swe_export_feedback_key,
        feedback_key=catalog.swe_export_feedback_key,
    )

    return Task(
        dataset=load_issue_dataset(
            dataset_name=catalog.fix.hf_dataset,
            dataset_version=dataset_version,
        ),
        solver=openbot_fix_solver(),
        scorer=exp.scorer,
        metadata={
            "dataset_version": dataset_version,
            "dataset_source": catalog.fix.dataset_source,
            "git_sha": sha,
            "solver_family": catalog.solver_family_baseline,
            "model": model_label,
            **exp.metadata,
        },
    )
