"""Schema + JSONL exporter tests for the structured-output contract."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from evals.data._predictions import (
    SweBenchPrediction,
    SweQaProAnswer,
    SweQaProCitation,
    SwtBenchPrediction,
    empty_swe_prediction,
    empty_swt_prediction,
    prediction_exporter,
    predictions_path,
)


def test_swe_bench_prediction_round_trips() -> None:
    p = SweBenchPrediction(
        instance_id="astropy__astropy-12907",
        model_name_or_path="anthropic:GLM-5.1",
        model_patch="diff --git a/foo b/foo\n+x\n",
    )
    blob = p.model_dump_json()
    parsed = SweBenchPrediction.model_validate_json(blob)
    assert parsed == p


def test_swt_bench_prediction_distinct_from_swe() -> None:
    # They share field names but they're distinct classes — we want callers
    # to opt into one or the other so future divergence can land safely.
    swt = SwtBenchPrediction(instance_id="x", model_name_or_path="m", model_patch="diff")
    swe = SweBenchPrediction(instance_id="x", model_name_or_path="m", model_patch="diff")
    assert type(swt) is not type(swe)
    assert swt.model_dump() == swe.model_dump()


def test_swe_qa_pro_answer_carries_citations() -> None:
    ans = SweQaProAnswer(
        answer="See routing.py",
        citations=[
            SweQaProCitation(relative_path="flask/routing.py", line_start=1, line_end=10),
        ],
    )
    blob = ans.model_dump()
    assert blob["answer"] == "See routing.py"
    assert blob["citations"][0]["relative_path"] == "flask/routing.py"


def test_extra_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SweBenchPrediction.model_validate(
            {
                "instance_id": "x",
                "model_name_or_path": "m",
                "model_patch": "",
                "unexpected": "boom",
            }
        )


def test_empty_predictions_have_empty_patch() -> None:
    swe = empty_swe_prediction(instance_id="x", model_name_or_path="m")
    swt = empty_swt_prediction(instance_id="x", model_name_or_path="m")
    assert swe.model_patch == ""
    assert swt.model_patch == ""


def test_predictions_path_layout(tmp_path: Path) -> None:
    p = predictions_path(
        dataset_version="fix_swe_bench_verified",
        run_label="exp-2026-05-16",
        root=tmp_path,
    )
    assert p == tmp_path / "fix_swe_bench_verified" / "exp-2026-05-16.predictions.jsonl"


@pytest.mark.asyncio
async def test_prediction_exporter_appends_validated_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENBOT_PREDICTIONS_DIR", str(tmp_path))
    scorer = prediction_exporter(
        dataset_version="fix_swe_bench_verified",
        schema=SweBenchPrediction,
        run_label="run-A",
    )
    pred = SweBenchPrediction(
        instance_id="x", model_name_or_path="m", model_patch="diff"
    ).model_dump()
    state = SimpleNamespace(metadata={"prediction": pred})

    score = await scorer(state, None)  # type: ignore[misc]
    assert score.value == 1

    out_file = tmp_path / "fix_swe_bench_verified" / "run-A.predictions.jsonl"
    assert out_file.exists()
    rows = [json.loads(line) for line in out_file.read_text().splitlines()]
    assert rows == [pred]


@pytest.mark.asyncio
async def test_prediction_exporter_reports_missing_prediction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENBOT_PREDICTIONS_DIR", str(tmp_path))
    scorer = prediction_exporter(
        dataset_version="fix_swe_bench_verified",
        schema=SweBenchPrediction,
        run_label="run-B",
    )
    state = SimpleNamespace(metadata={})
    score = await scorer(state, None)  # type: ignore[misc]
    assert score.value == 0
    assert "did not produce" in score.explanation


@pytest.mark.asyncio
async def test_prediction_exporter_scores_empty_patch_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: empty `model_patch` must not be exported nor scored 1.

    Schema-valid-but-empty rows used to silently score 1, making the
    LangSmith dashboard look like pass@1=1.0 even when the agent
    produced no edits (e.g. ran out of budget mid-investigation). The
    export sentinel must instead be 0 + an explanation, and the empty
    row must NOT be appended to the JSONL (running the upstream harness
    on empty rows just wastes time).
    """
    monkeypatch.setenv("OPENBOT_PREDICTIONS_DIR", str(tmp_path))
    scorer = prediction_exporter(
        dataset_version="fix_swe_bench_verified",
        schema=SweBenchPrediction,
        run_label="run-empty",
    )
    pred = SweBenchPrediction(
        instance_id="x", model_name_or_path="m", model_patch="   \n  "
    ).model_dump()
    state = SimpleNamespace(metadata={"prediction": pred})

    score = await scorer(state, None)  # type: ignore[misc]
    assert score.value == 0
    assert score.metadata["empty_patch"] is True
    assert "empty model_patch" in score.explanation

    out_file = tmp_path / "fix_swe_bench_verified" / "run-empty.predictions.jsonl"
    assert not out_file.exists()


@pytest.mark.asyncio
async def test_prediction_exporter_rejects_bad_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENBOT_PREDICTIONS_DIR", str(tmp_path))
    scorer = prediction_exporter(
        dataset_version="fix_swe_bench_verified",
        schema=SweBenchPrediction,
        run_label="run-C",
    )
    # Missing required ``model_patch`` field.
    state = SimpleNamespace(
        metadata={"prediction": {"instance_id": "x", "model_name_or_path": "m"}}
    )
    score = await scorer(state, None)  # type: ignore[misc]
    assert score.value == 0
    assert "schema" in score.explanation
