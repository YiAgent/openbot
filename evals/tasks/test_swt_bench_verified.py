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

import subprocess

from datasets import load_dataset
from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample

from evals.agents.baseline import resolve_model
from evals.common.prediction_export import prediction_exporter
from evals.common.predictions import SwtBenchPrediction
from evals.inspect.langsmith import LangSmithExperiment, configure_tracing_for_dataset
from evals.solvers.swe_test import deepagents_baseline_swt_solver

_DATASET_VERSION = "test_swt_bench_verified"
_HF_DATASET = "eth-sri/SWT-bench_Verified_bm25_27k_zsb"
_DATASET_SOURCE = f"huggingface:{_HF_DATASET}"


def _git_sha() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _resolve_model_label() -> str:
    return resolve_model()


def _hf_to_sample(row: dict[str, object]) -> Sample:
    """SWT-Bench eth-sri row → Inspect Sample.

    Mirrors the SWE-bench-Verified loader. Instance IDs match the SWE-bench
    Verified set (eth-sri only adds ``text``/``hits`` BM25 context which
    we don't need at the solver layer — the agent reads the repo directly).
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
    """Load eth-sri SWT-Bench Verified directly from HuggingFace."""
    ds = load_dataset(_HF_DATASET, split="test")
    samples = [_hf_to_sample(dict(row)) for row in ds]
    samples.sort(key=lambda s: str(s.id))
    return MemoryDataset(
        samples=samples,
        name=_DATASET_VERSION,
        location=f"huggingface://{_HF_DATASET}",
    )


@task
def test_swt_bench_verified_deepagents() -> Task:
    """SWT-Bench Verified with the deepagents solver on Modal.

    Produces ``predictions.jsonl`` in the SWT-Bench prediction format; the
    LangSmith Experiment row carries the schema-validation pass/fail signal.
    Run the upstream SWT-Bench Docker harness against the exported JSONL
    to obtain the leaderboard Success / Coverage metrics.
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
        schema=SwtBenchPrediction,
        run_label=str(experiment.metadata().get("langsmith_experiment_name") or "run"),
    )

    return Task(
        dataset=_load_dataset(),
        solver=deepagents_baseline_swt_solver(),
        scorer=experiment.wrap(
            exporter,
            # See fix_swe_bench_verified.py — this is an export sentinel,
            # not pass@1. Real grading is offline via SWT-Bench's grader.
            scorer_name="swt_export_ok",
            feedback_key="swt_export_ok",
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
