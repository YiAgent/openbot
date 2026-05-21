"""SWT-Bench Verified — PRD §3.1 (``test_swt_bench_verified`` cell).

Sister to :mod:`evals.tasks.fix_swe_bench_verified`. The architectural
contract is identical:

- The solver (:mod:`evals.solvers.swe_test`) runs deepagents in a Modal
  sandbox with the repo cloned at ``base_commit``, captures the agent's
  test-only patch via ``git diff``, and emits a
  :class:`SwtBenchPrediction` to ``state.metadata['prediction']``.
- The scorer is :func:`evals.common.prediction_export.prediction_exporter`,
  which appends a validated row to
  ``evals/outputs/test_swt_bench_verified/<run-label>.predictions.jsonl``.
- **Grading happens offline** by piping that JSONL to the official
  SWT-Bench Docker harness — see the SWT-Bench README. We deliberately do
  NOT run the six-pass pytest grid inside this task: that lives in the
  upstream harness, which we shouldn't duplicate (and any duplication
  drifts).

Run::

    doppler run --project openbot --config dev -- \\
        uv run inspect eval 'evals/tasks/test_swt_bench_verified.py' \\
        --limit 5

Set ``OPENBOT_DEEPAGENTS_MODEL`` once in the shared eval environment (or
prefix the command with ``env OPENBOT_DEEPAGENTS_MODEL=mimo-v2.5`` for a
one-off local override).
"""

from __future__ import annotations

from inspect_ai import Task, task

from evals.common.config import get_eval_config
from evals.common.predictions import SwtBenchPrediction
from evals.inspect.hf_datasets import load_issue_dataset
from evals.inspect.langsmith import configure_tracing_for_dataset
from evals.inspect.task_runtime import build_export_experiment, git_sha, resolve_model_label
from evals.solvers.swe_test import deepagents_baseline_swt_solver


@task
def test_swt_bench_verified_deepagents() -> Task:
    """SWT-Bench Verified with the deepagents solver on Modal.

    Produces ``predictions.jsonl`` in the SWT-Bench prediction format; the
    LangSmith Experiment row carries the schema-validation pass/fail signal.
    Run the upstream SWT-Bench Docker harness against the exported JSONL
    to obtain the leaderboard Success / Coverage metrics.
    """
    catalog = get_eval_config().catalog
    dataset_version = catalog.swt.dataset_version
    configure_tracing_for_dataset(dataset_version)

    sha = git_sha()
    model_label = resolve_model_label()

    exp = build_export_experiment(
        dataset_version=dataset_version,
        solver_family=catalog.solver_family_baseline,
        model=model_label,
        git_sha=sha,
        schema=SwtBenchPrediction,
        # See fix_swe_bench_verified.py — this is an export sentinel,
        # not pass@1. Real grading is offline via SWT-Bench's grader.
        scorer_name=catalog.swt_export_feedback_key,
        feedback_key=catalog.swt_export_feedback_key,
    )

    return Task(
        dataset=load_issue_dataset(
            dataset_name=catalog.swt.hf_dataset,
            dataset_version=dataset_version,
        ),
        solver=deepagents_baseline_swt_solver(),
        scorer=exp.scorer,
        metadata={
            "dataset_version": dataset_version,
            "dataset_source": f"huggingface:{catalog.swt.hf_dataset}",
            "git_sha": sha,
            "solver_family": catalog.solver_family_baseline,
            "model": model_label,
            **exp.metadata,
        },
    )
