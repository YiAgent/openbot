"""Unit tests for evals.common.langsmith — PRD §10.1 / §10.2 contract guard."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from evals.common import langsmith as ls
from evals.common._metadata_spec import (
    REQUIRED_RUN_FIELD_NAMES,
    REQUIRED_SAMPLE_FIELD_NAMES,
)


def _full_metadata() -> dict[str, Any]:
    """Build a minimally-complete §10.1 metadata dict — every required key, dummy values."""
    return {
        "suite_name": "review_martian",
        "suite_version": 1,
        "dataset_version": "martian_review_v1",
        "dataset_sha256": "0" * 64,
        "git_sha": "abc1234",
        "prompt_version": 1,
        "workflow_version": 1,
        "model_id": "anthropic/claude-opus-4-7",
        "judge_model_id": "anthropic/claude-opus-4-7",
        "judge_prompt_version": 1,
        "sandbox_backend": "modal",
        "runner_version": "0.3.220",
        "mode": "smoke",
        "started_at": "2026-05-15T00:00:00Z",
        "triggered_by": "local",
    }


def _full_sample() -> dict[str, Any]:
    """Build a minimally-complete §10.2 sample dict."""
    return {
        "sample_id": "martian-pr-#1",
        "input_artifact_refs": [],
        "output_artifact_refs": [],
        "score_payload": {"f1": 0.7},
        "tokens_in": 100,
        "tokens_out": 50,
        "cost_usd": 0.01,
        "latency_ms": 1200,
        "step_count": 4,
        "tool_call_count": 2,
        "retry_count": 0,
        "sandbox_restart_count": 0,
        "failure_category": "none",
    }


# ─── init_client env handling ────────────────────────────────────────────────


def test_init_client_raises_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
        ls.init_client()


def test_init_client_raises_when_internal_project_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.delenv("LANGSMITH_PROJECT_INTERNAL", raising=False)
    with pytest.raises(ValueError, match="LANGSMITH_PROJECT_INTERNAL"):
        ls.init_client(public=False)


def test_init_client_raises_when_public_project_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.delenv("LANGSMITH_PROJECT_PUBLIC", raising=False)
    with pytest.raises(ValueError, match="LANGSMITH_PROJECT_PUBLIC"):
        ls.init_client(public=True)


# ─── log_run_metadata: PRD §10.1 — 15 required fields ────────────────────────


def test_log_run_metadata_with_all_fields_passes() -> None:
    client = MagicMock()
    ls.log_run_metadata(client, "run-1", _full_metadata())
    client.update_run.assert_called_once()


@pytest.mark.parametrize("missing_field", sorted(REQUIRED_RUN_FIELD_NAMES))
def test_log_run_metadata_raises_when_any_required_field_missing(
    missing_field: str,
) -> None:
    """Drop exactly one required field and confirm the guard catches it.

    Parametrized over every PRD §10.1 field — if a field is added to the spec,
    this test automatically covers it without edits here.
    """
    metadata = _full_metadata()
    del metadata[missing_field]
    client = MagicMock()
    with pytest.raises(ValueError, match=missing_field):
        ls.log_run_metadata(client, "run-1", metadata)
    client.update_run.assert_not_called()


def test_log_run_metadata_field_count_matches_prd_spec() -> None:
    """Hard pin the field count at 15 — drift fails this test loudly."""
    assert len(REQUIRED_RUN_FIELD_NAMES) == 15


# ─── log_sample: PRD §10.2 — 13 required fields ──────────────────────────────


def test_log_sample_with_all_fields_passes() -> None:
    client = MagicMock()
    ls.log_sample(client, "run-1", _full_sample())
    client.create_feedback.assert_called_once()


@pytest.mark.parametrize("missing_field", sorted(REQUIRED_SAMPLE_FIELD_NAMES))
def test_log_sample_raises_when_any_required_field_missing(missing_field: str) -> None:
    sample = _full_sample()
    del sample[missing_field]
    client = MagicMock()
    with pytest.raises(ValueError, match=missing_field):
        ls.log_sample(client, "run-1", sample)
    client.create_feedback.assert_not_called()


def test_log_sample_field_count_matches_prd_spec() -> None:
    """Hard pin the §10.2 field count at 13."""
    assert len(REQUIRED_SAMPLE_FIELD_NAMES) == 13


# ─── Source-of-truth alignment: script vs library ────────────────────────────


def test_validator_script_imports_same_required_fields() -> None:
    """`scripts/validate_langsmith_run.py --help` must enumerate the same fields.

    Both surfaces import from `evals.common._metadata_spec`, so this is mostly
    a wiring assertion. Drifting them is a governance bug.
    """
    import importlib.util
    import pathlib

    script = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "scripts"
        / "validate_langsmith_run.py"
    )
    spec = importlib.util.spec_from_file_location("validate_langsmith_run", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {n for n, _ in module.REQUIRED_RUN_METADATA_FIELDS} == REQUIRED_RUN_FIELD_NAMES
