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

import subprocess

from datasets import load_dataset
from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample

from evals.agents.baseline import resolve_model
from evals.agents.langsmith import LangSmithExperiment, configure_tracing_for_dataset
from evals.common.prediction_export import prediction_exporter
from evals.common.predictions import SweBenchPrediction
from evals.solvers.swe_fix import deepagents_baseline_swe_solver

_DATASET_VERSION = "fix_swe_bench_verified"
_DATASET_SOURCE = "huggingface:princeton-nlp/SWE-bench_Verified"


def _git_sha() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _resolve_model_label() -> str:
    """Best-effort model label for experiment metadata."""
    return resolve_model()


def _hf_to_sample(row: dict[str, object]) -> Sample:
    """SWE-bench Verified HF row → Inspect ``Sample``.

    Only the fields the solver actually consumes are surfaced into
    ``state.metadata`` — repo, base_commit, version. Everything else
    (FAIL_TO_PASS / PASS_TO_PASS / patch / test_patch / environment_setup_commit)
    is grading-side data and stays on the official harness; we don't need
    to mirror it into our flow.
    """
    return Sample(
        id=str(row["instance_id"]),
        input=str(row.get("problem_statement", "")),
        target="",
        metadata={
            "repo": str(row.get("repo", "")),
            "base_commit": str(row.get("base_commit", "")),
            "version": str(row.get("version", "")),
        },
    )


def _load_dataset() -> MemoryDataset:
    """Pull SWE-bench Verified directly from HuggingFace.

    Bypassing LangSmith for SWE/SWT-Bench is deliberate: the upstream
    Docker harness already wants the official HF schema, so re-mirroring
    through LangSmith adds drift surface without benefit. (LangSmith
    Experiment records remain wired through the scorer wrapper.)
    """
    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    samples = [_hf_to_sample(dict(row)) for row in ds]
    samples.sort(key=lambda s: str(s.id))
    return MemoryDataset(
        samples=samples,
        name=_DATASET_VERSION,
        location="huggingface://princeton-nlp/SWE-bench_Verified",
    )


@task
def fix_swe_bench_verified_deepagents() -> Task:
    """SWE-bench Verified with the deepagents solver on Modal.

    Produces ``predictions.jsonl`` in the official SWE-bench format; the
    LangSmith Experiment row carries the schema-validation pass/fail signal
    (value=1 if the prediction validated and was appended, value=0 if the
    solver returned an unusable prediction). Real grading happens offline.
    """
    configure_tracing_for_dataset(_DATASET_VERSION)
    git_sha = _git_sha()
    model_label = _resolve_model_label()

    experiment = LangSmithExperiment.start(
        dataset_name=_DATASET_VERSION,
        solver_family="deepagents_baseline",
        model=model_label,
        git_sha=git_sha,
    )

    exporter = prediction_exporter(
        dataset_version=_DATASET_VERSION,
        schema=SweBenchPrediction,
        run_label=str(experiment.metadata().get("langsmith_experiment_name") or "run"),
    )

    return Task(
        dataset=_load_dataset(),
        solver=deepagents_baseline_swe_solver(),
        scorer=experiment.wrap(
            exporter,
            # IMPORTANT: this is NOT official pass@1. The scorer is a
            # JSONL exporter; value=1 means "non-empty prediction appended",
            # value=0 means "no prediction / empty patch / schema invalid".
            # Real grading is offline via `python -m swebench.harness.run_evaluation`.
            scorer_name="swe_export_ok",
            feedback_key="swe_export_ok",
            feedback_config={"type": "continuous", "min": 0.0, "max": 1.0},
        ),
        metadata={
            "dataset_version": _DATASET_VERSION,
            "dataset_source": _DATASET_SOURCE,
            "git_sha": git_sha,
            "solver_family": "deepagents_baseline",
            "model": model_label,
            **experiment.metadata(),
        },
    )
