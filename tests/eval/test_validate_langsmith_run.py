"""Tests for the real LangSmith run validator."""

from __future__ import annotations

import importlib.util
import pathlib
from dataclasses import dataclass
from typing import Any

import pytest


def _load_validator_module():
    script = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "scripts"
        / "validate_langsmith_run.py"
    )
    spec = importlib.util.spec_from_file_location("validate_langsmith_run", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class _FakeRun:
    name: str
    extra: dict[str, Any]


def _metadata() -> dict[str, Any]:
    return {
        "suite_name": "review_martian",
        "suite_version": 1,
        "dataset_version": "martian_smoke_v1",
        "dataset_sha256": "0" * 64,
        "git_sha": "abcdef123456",
        "prompt_version": "unknown",
        "workflow_version": "unknown",
        "solver_id": "deepagents_baseline",
        "solver_family": "baseline",
        "model_id": "anthropic/claude-opus-4-7",
        "judge_model_id": "anthropic/claude-opus-4-7",
        "judge_prompt_version": 1,
        "sandbox_backend": "modal",
        "runner_version": "0.3.220",
        "mode": "smoke",
        "started_at": "2026-05-15T00:00:00Z",
        "triggered_by": "local",
    }


def test_validator_rejects_missing_metadata() -> None:
    validator = _load_validator_module()
    run = _FakeRun(
        name="review-martian_smoke_v1-abcdef-opus47-smoke",
        extra={"metadata": {"suite_name": "review_martian"}},
    )

    with pytest.raises(ValueError, match="missing required"):
        validator.validate_run(run)


def test_validator_rejects_bad_experiment_name() -> None:
    validator = _load_validator_module()
    run = _FakeRun(name="bad-name", extra={"metadata": _metadata()})

    with pytest.raises(ValueError, match="experiment name"):
        validator.validate_run(run)


def test_validator_accepts_valid_run() -> None:
    validator = _load_validator_module()
    run = _FakeRun(
        name="review-martian_smoke_v1-abcdef-opus47-smoke",
        extra={"metadata": _metadata()},
    )

    assert validator.validate_run(run) is None
