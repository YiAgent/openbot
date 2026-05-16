"""Contract tests for the SWE-QA-Pro Inspect scorer wrapper."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from inspect_ai.scorer import Target

from evals.scorers import swe_qa_pro


def _state(*, completion: str, metadata: dict | None = None):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        input_text="What does the router do?",
        output=SimpleNamespace(completion=completion),
        metadata=metadata or {},
    )


@pytest.mark.asyncio
async def test_scorer_normalizes_overall_and_preserves_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        swe_qa_pro,
        "judge_answer",
        lambda **_: {
            "scores": {
                "correctness": 8,
                "completeness": 7,
                "relevance": 9,
                "clarity": 6,
                "reasoning": 5,
            },
            "overall": 35,
            "rl_reward": 0.72,
        },
    )

    score = await swe_qa_pro.swe_qa_pro_judge_scorer()(
        _state(completion="candidate"),
        Target("reference"),
    )

    assert score.value == pytest.approx(0.7)
    assert score.answer == "candidate"
    assert score.metadata == {
        "correctness": 8,
        "completeness": 7,
        "relevance": 9,
        "clarity": 6,
        "reasoning": 5,
        "overall_50": 35,
        "rl_reward": 0.72,
        "judge_model_id": swe_qa_pro.SWE_QA_JUDGE_MODEL_ID,
        "judge_version": swe_qa_pro.SWE_QA_JUDGE_VERSION,
        "judge_deviations": list(swe_qa_pro.JUDGE_DEVIATIONS),
        "langsmith_run_id": None,
    }
    assert "overall=35/50" in score.explanation


@pytest.mark.asyncio
async def test_scorer_posts_feedback_when_trace_id_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        swe_qa_pro,
        "judge_answer",
        lambda **_: {
            "scores": {
                "correctness": 10,
                "completeness": 9,
                "relevance": 8,
                "clarity": 7,
                "reasoning": 6,
            },
            "overall": 40,
            "rl_reward": 0.84,
        },
    )
    seen: dict[str, object] = {}

    def _capture_feedback(**kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)

    monkeypatch.setattr(swe_qa_pro, "_post_feedback_to_langsmith", _capture_feedback)

    score = await swe_qa_pro.swe_qa_pro_judge_scorer()(
        _state(completion="candidate", metadata={"langsmith_run_id": "run-123"}),
        Target("reference"),
    )

    assert seen == {
        "extra": None,
        "reference_answer": "reference",
        "run_id": "run-123",
        "session_id": None,
        "scores": {
            "correctness": 10,
            "completeness": 9,
            "relevance": 8,
            "clarity": 7,
            "reasoning": 6,
        },
        "source_info": {"source": "inspect_scorer", "scorer_name": "swe_qa_pro_judge"},
        "overall": 40,
        "rl_reward": 0.84,
    }
    assert score.metadata["langsmith_run_id"] == "run-123"


def test_post_feedback_emits_per_dimension_overall_and_reward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class _Client:
        def create_feedback(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(Client=lambda: _Client()))

    swe_qa_pro._post_feedback_to_langsmith(
        run_id="run-123",
        scores={"correctness": 8, "completeness": 7},
        overall=15,
        rl_reward=0.75,
        reference_answer="reference answer",
        session_id="project-123",
        source_info={"source": "inspect_scorer"},
        extra={"provider_usage": {"input_tokens": 9, "total_cost": 0.12}},
    )

    assert calls == [
        {
            "trace_id": "run-123",
            "session_id": "project-123",
            "key": "swe_qa_pro_correctness",
            "score": 8.0,
            "value": 8,
            "comment": f"Appendix D rubric, 1-10 (judge v{swe_qa_pro.SWE_QA_JUDGE_VERSION})",
            "source_info": {
                "source": "inspect_scorer",
                "judge_model_id": swe_qa_pro.SWE_QA_JUDGE_MODEL_ID,
                "judge_version": swe_qa_pro.SWE_QA_JUDGE_VERSION,
            },
            "feedback_config": {"type": "continuous", "min": 1.0, "max": 10.0},
            "extra": {"provider_usage": {"input_tokens": 9, "total_cost": 0.12}},
        },
        {
            "trace_id": "run-123",
            "session_id": "project-123",
            "key": "swe_qa_pro_completeness",
            "score": 7.0,
            "value": 7,
            "comment": f"Appendix D rubric, 1-10 (judge v{swe_qa_pro.SWE_QA_JUDGE_VERSION})",
            "source_info": {
                "source": "inspect_scorer",
                "judge_model_id": swe_qa_pro.SWE_QA_JUDGE_MODEL_ID,
                "judge_version": swe_qa_pro.SWE_QA_JUDGE_VERSION,
            },
            "feedback_config": {"type": "continuous", "min": 1.0, "max": 10.0},
            "extra": {"provider_usage": {"input_tokens": 9, "total_cost": 0.12}},
        },
        {
            "trace_id": "run-123",
            "session_id": "project-123",
            "key": "swe_qa_pro_overall_50",
            "score": 0.3,
            "value": 15,
            "comment": f"sum of 5 dims (paper Table 2); judge v{swe_qa_pro.SWE_QA_JUDGE_VERSION}",
            "correction": {"reference_answer": "reference answer"},
            "source_info": {
                "source": "inspect_scorer",
                "judge_model_id": swe_qa_pro.SWE_QA_JUDGE_MODEL_ID,
                "judge_version": swe_qa_pro.SWE_QA_JUDGE_VERSION,
            },
            "feedback_config": {"type": "continuous", "min": 0.0, "max": 1.0},
            "extra": {"provider_usage": {"input_tokens": 9, "total_cost": 0.12}},
        },
        {
            "trace_id": "run-123",
            "session_id": "project-123",
            "key": "swe_qa_pro_rl_reward",
            "score": 0.75,
            "comment": "paper §3.2 Eq. (4): r = w · s / 10; RL only, not eval metric",
            "source_info": {
                "source": "inspect_scorer",
                "judge_model_id": swe_qa_pro.SWE_QA_JUDGE_MODEL_ID,
                "judge_version": swe_qa_pro.SWE_QA_JUDGE_VERSION,
            },
            "feedback_config": {"type": "continuous", "min": 0.0, "max": 1.0},
            "extra": {"provider_usage": {"input_tokens": 9, "total_cost": 0.12}},
        },
    ]
