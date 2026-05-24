"""Shared Inspect task-construction helpers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any

from inspect_ai.scorer import Scorer
from pydantic import BaseModel

from evals.agents.baseline import resolve_model
from evals.common.config import get_eval_config
from evals.common.prediction_export import prediction_exporter
from evals.inspect.langsmith import LangSmithExperiment


@dataclass(frozen=True)
class ExportExperiment:
    """Prediction-export scorer plus metadata for an Inspect task."""

    scorer: Scorer
    metadata: dict[str, Any]


def git_sha() -> str:
    """Best-effort current git SHA for run metadata."""
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def resolve_model_label() -> str:
    """Best-effort model label for task and experiment metadata."""
    return resolve_model()


def build_export_experiment(
    *,
    dataset_version: str,
    solver_family: str,
    model: str,
    git_sha: str,
    schema: type[BaseModel],
    scorer_name: str,
    feedback_key: str,
) -> ExportExperiment:
    """Build the LangSmith-wrapped prediction exporter used by patch tasks."""
    experiment = LangSmithExperiment.start(
        dataset_name=dataset_version,
        solver_family=solver_family,
        model=model,
        git_sha=git_sha,
    )
    experiment_metadata = experiment.metadata()
    exporter = prediction_exporter(
        dataset_version=dataset_version,
        schema=schema,
        run_label=str(experiment_metadata.get("langsmith_experiment_name") or "run"),
        scorer_name=scorer_name,
    )
    return ExportExperiment(
        scorer=experiment.wrap(
            exporter,
            scorer_name=scorer_name,
            feedback_key=feedback_key,
            feedback_config=get_eval_config().catalog.unit_feedback_config,
        ),
        metadata=experiment_metadata,
    )
