"""SWT-Bench Verified — PRD §3.1 (``test_swt_bench`` eval surface).

The solver emits an explicit unsupported-capability prediction until OpenBot
has a real test-generation capability (see :mod:`evals.solvers.test_generation`).
Grading is offline via the SWT-Bench Docker harness.

Run::

    doppler run --project openbot --config dev -- \\
        uv run inspect eval 'evals/tasks/test_swt_bench.py' --limit 5
"""

from __future__ import annotations

from inspect_ai import Task, task

from evals.runtime.config import get_eval_config
from evals.runtime.datasets import load_issue_dataset
from evals.runtime.environment import build_export_experiment, git_sha, resolve_model_label
from evals.runtime.langsmith import configure_tracing_for_dataset
from evals.runtime.predictions import SwtBenchPrediction
from evals.solvers.test_generation import openbot_test_generation_solver


@task
def test_swt_bench() -> Task:
    """SWT-Bench Verified with the OpenBot test-generation solver.

    Until test generation is implemented, each sample emits an empty
    SwtBenchPrediction with ``unsupported=true`` metadata. LangSmith rows
    remain stable.
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
        scorer_name=catalog.swt_export_feedback_key,
        feedback_key=catalog.swt_export_feedback_key,
    )

    return Task(
        dataset=load_issue_dataset(
            dataset_name=catalog.swt.hf_dataset,
            dataset_version=dataset_version,
        ),
        solver=openbot_test_generation_solver(),
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
