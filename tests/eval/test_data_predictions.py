"""Smoke tests for evals/data package: _predictions, _samples, and _utils APIs."""

from __future__ import annotations

import pytest

from evals.data import _predictions, _samples

PRED_NAMES = [
    "SweBenchPrediction",
    "SwtBenchPrediction",
    "SweQaProAnswer",
    "SweQaProCitation",
    "empty_swe_prediction",
    "empty_swt_prediction",
    "predictions_path",
    "prediction_exporter",
]
SAMPLE_NAMES = [
    "review_example_to_sample",
    "qa_example_to_sample",
    "qa_example_to_agent_sample",
    "langsmith_dataset",
    "load_issue_dataset",
    "issue_row_to_sample",
]


@pytest.mark.parametrize("name", PRED_NAMES)
def test_predictions_api(name: str) -> None:
    assert hasattr(_predictions, name)


@pytest.mark.parametrize("name", SAMPLE_NAMES)
def test_samples_api(name: str) -> None:
    assert hasattr(_samples, name)
