"""Contract tests for the Inspect -> LangSmith experiment bridge."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from inspect_ai.scorer import Score

from evals.common.langsmith_experiments import LangSmithExperiment, _await_or_call


def test_start_without_api_key_returns_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)

    exp = LangSmithExperiment.start(dataset_name="fix", solver_family="baseline")

    assert exp.project_id is None
    assert exp.dataset_id is None
    assert exp.instance_to_example == {}


def test_start_with_missing_dataset_returns_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")

    class _Client:
        def list_datasets(self, *, dataset_name: str):  # type: ignore[no-untyped-def]
            assert dataset_name == "fix"
            return []

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(Client=lambda: _Client()))

    exp = LangSmithExperiment.start(dataset_name="fix", solver_family="baseline")

    assert exp.project_id is None
    assert exp.dataset_id is None


def test_start_indexes_dataset_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")

    class _Client:
        def list_datasets(self, *, dataset_name: str):  # type: ignore[no-untyped-def]
            assert dataset_name == "fix"
            return [SimpleNamespace(id="dataset-id")]

        def create_project(self, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs["reference_dataset_id"] == "dataset-id"
            return SimpleNamespace(id="project-id")

        def list_examples(self, *, dataset_id: str):  # type: ignore[no-untyped-def]
            assert dataset_id == "dataset-id"
            return [
                SimpleNamespace(id="ex-1", inputs={"instance_id": "i-1"}, metadata={}),
                SimpleNamespace(id="ex-2", inputs={}, metadata={"instance_id": "i-2"}),
            ]

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(Client=lambda: _Client()))

    exp = LangSmithExperiment.start(dataset_name="fix", solver_family="baseline")
    exp._provision()

    assert exp.project_id == "project-id"
    assert exp.dataset_id == "dataset-id"
    assert exp.instance_to_example == {"i-1": "ex-1", "i-2": "ex-2"}


def test_post_run_emits_run_and_feedback_when_example_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, list[dict[str, object]]] = {"runs": [], "feedback": []}

    class _Client:
        def create_run(self, **kwargs):  # type: ignore[no-untyped-def]
            calls["runs"].append(kwargs)

        def create_feedback(self, **kwargs):  # type: ignore[no-untyped-def]
            calls["feedback"].append(kwargs)

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(Client=lambda: _Client()))
    exp = LangSmithExperiment(
        dataset_name="fix",
        experiment_name="exp",
        solver_family="baseline",
        model=None,
        instance_id_field="instance_id",
        enabled=True,
        project_id="project-id",
        dataset_id="dataset-id",
        instance_to_example={"i-1": "ex-1"},
        extra_metadata={"solver_family": "baseline"},
        _initialized=True,
    )

    exp._post_run(
        instance_id="i-1",
        score=Score(
            value=0.75,
            explanation="ok",
            metadata={
                "model_patch": "diff",
                "precision": 0.6,
                "recall": 1.0,
                "f1": 0.75,
            },
        ),
        sample_input="issue",
        candidate_completion='{"findings":[{"file":"a.py","line":1,"body":"bug","severity":"high"}]}',
        sample_metadata={
            "repo": "demo",
            "provider_usage": {
                "input_tokens": 12,
                "output_tokens": 4,
                "total_cost": 0.42,
                "input_cost": 0.3,
                "output_cost": 0.12,
                "input_token_details": {"cache_read": 2},
                "output_token_details": {"reasoning": 1},
            },
            "langsmith_run_id": "trace-upstream-1",
        },
        scorer_name="review_overlap_f1",
        feedback_key="review_overlap_f1",
    )

    assert len(calls["runs"]) == 1
    assert calls["runs"][0]["reference_example_id"] == "ex-1"
    assert calls["runs"][0]["run_type"] == "llm"
    assert calls["runs"][0]["prompt_tokens"] == 12
    assert calls["runs"][0]["completion_tokens"] == 4
    assert calls["runs"][0]["total_tokens"] == 16
    assert calls["runs"][0]["total_cost"] == 0.42
    assert calls["runs"][0]["prompt_cost"] == 0.3
    assert calls["runs"][0]["completion_cost"] == 0.12
    assert calls["runs"][0]["prompt_token_details"] == {"cache_read": 2}
    assert calls["runs"][0]["completion_token_details"] == {"reasoning": 1}
    assert calls["runs"][0]["outputs"] == {
        "score": 0.75,
        "model_patch": "diff",
        "completion": {
            "findings": [{"file": "a.py", "line": 1, "body": "bug", "severity": "high"}]
        },
    }
    assert calls["feedback"] == [
        {
            "run_id": calls["runs"][0]["id"],
            "trace_id": calls["runs"][0]["id"],
            "session_id": "project-id",
            "key": "review_overlap_f1",
            "score": 0.75,
            "value": 0.75,
            "comment": "ok",
            "source_info": {"source": "inspect_scorer", "scorer_name": "review_overlap_f1"},
            "extra": {
                "metadata": {
                    "metric_name": "review_overlap_f1",
                    "score_metadata": {
                        "model_patch": "diff",
                        "precision": 0.6,
                        "recall": 1.0,
                        "f1": 0.75,
                    },
                    "sample_metadata": {
                        "repo": "demo",
                        "provider_usage": {
                            "input_tokens": 12,
                            "output_tokens": 4,
                            "total_cost": 0.42,
                            "input_cost": 0.3,
                            "output_cost": 0.12,
                            "input_token_details": {"cache_read": 2},
                            "output_token_details": {"reasoning": 1},
                        },
                        "langsmith_run_id": "trace-upstream-1",
                    },
                }
            },
            "start_time": calls["runs"][0]["start_time"],
        },
        {
            "run_id": calls["runs"][0]["id"],
            "trace_id": calls["runs"][0]["id"],
            "session_id": "project-id",
            "key": "review_overlap_precision",
            "score": 0.6,
            "value": 0.6,
            "comment": "ok",
            "source_info": {"source": "inspect_scorer", "scorer_name": "review_overlap_f1"},
            "extra": {"metadata": {"metric_name": "review_overlap_precision"}},
            "start_time": calls["runs"][0]["start_time"],
        },
        {
            "run_id": calls["runs"][0]["id"],
            "trace_id": calls["runs"][0]["id"],
            "session_id": "project-id",
            "key": "review_overlap_recall",
            "score": 1.0,
            "value": 1.0,
            "comment": "ok",
            "source_info": {"source": "inspect_scorer", "scorer_name": "review_overlap_f1"},
            "extra": {"metadata": {"metric_name": "review_overlap_recall"}},
            "start_time": calls["runs"][0]["start_time"],
        },
    ]


def test_post_run_skips_when_example_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "langsmith",
        SimpleNamespace(
            Client=lambda: (_ for _ in ()).throw(AssertionError("should not instantiate"))
        ),
    )
    exp = LangSmithExperiment(
        dataset_name="fix",
        experiment_name="exp",
        solver_family="baseline",
        model=None,
        instance_id_field="instance_id",
        enabled=True,
        project_id="project-id",
        dataset_id="dataset-id",
        instance_to_example={},
        _initialized=True,
    )

    exp._post_run(
        instance_id="missing",
        score=Score(value=0.0),
        sample_input="issue",
        candidate_completion=None,
        sample_metadata=None,
        scorer_name="swe_bench_scorer",
    )


@pytest.mark.asyncio
async def test_await_or_call_rejects_non_score() -> None:
    async def _bad(*_args):  # type: ignore[no-untyped-def]
        return "not-a-score"

    with pytest.raises(TypeError, match="expected Score"):
        await _await_or_call(_bad, None, None)  # type: ignore[arg-type]
