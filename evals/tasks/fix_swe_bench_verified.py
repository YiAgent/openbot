"""SWE-bench Verified — PRD §3.1 (``fix_swe_bench_verified`` cell).

Post-refactor architecture:

- Solver runs in its own **Modal sandbox** with the repo cloned at
  ``base_commit`` (see :mod:`evals.solvers.swe_fix`).
- The "scorer" wired here is :func:`evals.common.prediction_export.prediction_exporter`,
  which validates the agent's :class:`SweBenchPrediction` against the
  official schema and appends it to
  ``evals/outputs/fix_swe_bench_verified/<run-label>.predictions.jsonl``.
- **Actual grading happens offline** by piping that JSONL to the upstream
  SWE-bench Docker harness:

    python -m swebench.harness.run_evaluation \\
        --dataset_name princeton-nlp/SWE-bench_Verified \\
        --predictions_path evals/outputs/fix_swe_bench_verified/<run>.predictions.jsonl \\
        --max_workers 8 --run_id openbot-{date}

The Inspect Task is therefore *only* responsible for orchestrating the
agent runs and stamping LangSmith Experiment metadata — not for grading.

Run::

    doppler run --project openbot --config dev -- \\
        uv run inspect eval 'evals/tasks/fix_swe_bench_verified.py' \\
        --limit 5

Set ``OPENBOT_DEEPAGENTS_MODEL`` once in the shared eval environment (or
prefix the command with ``env OPENBOT_DEEPAGENTS_MODEL=mimo-v2.5`` for a
one-off local override).
"""

from __future__ import annotations

from inspect_ai import Task, task

from evals.common.config import get_eval_config
from evals.common.predictions import SweBenchPrediction
from evals.inspect.hf_datasets import load_issue_dataset
from evals.inspect.langsmith import configure_tracing_for_dataset
from evals.inspect.task_runtime import build_export_experiment, git_sha, resolve_model_label
from evals.solvers.swe_fix import deepagents_baseline_swe_solver


@task
def fix_swe_bench_verified_deepagents() -> Task:
    """SWE-bench Verified with the deepagents solver on Modal.

    Produces ``predictions.jsonl`` in the official SWE-bench format; the
    LangSmith Experiment row carries the schema-validation pass/fail signal
    (value=1 if the prediction validated and was appended, value=0 if the
    solver returned an unusable prediction). Real grading happens offline.
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
        # IMPORTANT: this is NOT official pass@1. The scorer is a
        # JSONL exporter; value=1 means "non-empty prediction appended",
        # value=0 means "no prediction / empty patch / schema invalid".
        # Real grading is offline via `python -m swebench.harness.run_evaluation`.
        scorer_name=catalog.swe_export_feedback_key,
        feedback_key=catalog.swe_export_feedback_key,
    )

    return Task(
        dataset=load_issue_dataset(
            dataset_name=catalog.fix.hf_dataset,
            dataset_version=dataset_version,
        ),
        solver=deepagents_baseline_swe_solver(),
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
