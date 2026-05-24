"""Shared Inspect task-construction helpers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any

from inspect_ai.scorer import Scorer
from pydantic import BaseModel

from evals.runtime.config import get_eval_config
from evals.runtime.langsmith import LangSmithExperiment
from evals.runtime.predictions import prediction_exporter


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
    """Best-effort model label for task and experiment metadata.

    Returns the OpenBot production model identifier. After the Phase 2 refactor,
    the model is determined by ``openbot.core.settings`` — the label here is
    metadata-only (LangSmith / export), not a routing decision.
    """
    try:
        from openbot.core.settings import Settings

        return Settings().model or "openbot"
    except Exception:
        return "openbot"


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
