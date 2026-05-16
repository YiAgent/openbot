"""Contract tests for the SWE-QA-Pro dev judge surface."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evals.scorers import swe_qa_judge


class _StructuredJudge:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.messages: list[object] | None = None

    def invoke(self, messages):  # type: ignore[no-untyped-def]
        self.messages = messages
        if self.error is not None:
            raise self.error
        return self.response


class _JudgeClient:
    def __init__(self, structured: _StructuredJudge) -> None:
        self.structured = structured
        self.schemas: list[type[object]] = []
        self.structured_kwargs: list[dict[str, object]] = []

    def with_structured_output(self, schema, **kwargs):  # type: ignore[no-untyped-def]
        self.schemas.append(schema)
        self.structured_kwargs.append(kwargs)
        return self.structured


def test_judge_prompt_is_single_official_user_message(monkeypatch: pytest.MonkeyPatch) -> None:
    structured = _StructuredJudge(
        response=SimpleNamespace(
            correctness=8,
            completeness=7,
            relevance=9,
            clarity=8,
            reasoning=7,
        )
    )
    client = _JudgeClient(structured)
    monkeypatch.setattr(swe_qa_judge, "_get_client", lambda: client)

    verdict = swe_qa_judge.judge_answer(
        question="What does the router do?",
        reference="It routes requests.",
        candidate="It routes requests to handlers.",
    )

    assert verdict["scores"] == {
        "correctness": 8,
        "completeness": 7,
        "relevance": 9,
        "clarity": 8,
        "reasoning": 7,
    }
    assert client.schemas == [swe_qa_judge.SWEQAScorecard]
    assert client.structured_kwargs == [{"method": "json_schema"}]
    assert structured.messages == [
        {
            "role": "user",
            "content": swe_qa_judge.SWE_QA_JUDGE_PROMPT.format(
                question="What does the router do?",
                reference="It routes requests.",
                candidate="It routes requests to handlers.",
            ),
        }
    ]


def test_invalid_structured_judge_output_is_not_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    structured = _StructuredJudge(error=ValueError("invalid structured output"))
    client = _JudgeClient(structured)
    monkeypatch.setattr(swe_qa_judge, "_get_client", lambda: client)

    with pytest.raises(ValueError, match="invalid structured output"):
        swe_qa_judge.judge_answer(
            question="q",
            reference="r",
            candidate="c",
        )


def test_overall_score_sums_the_five_dimensions() -> None:
    scores = {
        "correctness": 8,
        "completeness": 7,
        "relevance": 9,
        "clarity": 6,
        "reasoning": 5,
    }

    assert swe_qa_judge.overall_score(scores) == 35


def test_weighted_rl_reward_uses_paper_weights() -> None:
    scores = {
        "correctness": 10,
        "completeness": 8,
        "relevance": 6,
        "clarity": 4,
        "reasoning": 2,
    }

    assert swe_qa_judge.weighted_rl_reward(scores) == pytest.approx(0.66)
