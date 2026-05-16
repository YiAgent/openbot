"""Usage extraction tests for the deepagents baseline provider."""

from __future__ import annotations

from dataclasses import dataclass

from evals.solvers.review import _extract_provider_usage, _parse_agent_result


@dataclass
class _MessageWithUsage:
    usage_metadata: dict[str, int | float]


@dataclass
class _MessageWithResponseMetadata:
    response_metadata: dict[str, dict[str, int | float]]


def test_extract_provider_usage_prefers_usage_metadata() -> None:
    msg = _MessageWithUsage({"input_tokens": 10, "output_tokens": 5, "total_cost": 0.1})
    assert _extract_provider_usage(msg) == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_cost": 0.1,
    }


def test_extract_provider_usage_falls_back_to_response_metadata_usage() -> None:
    msg = _MessageWithResponseMetadata({"usage": {"input_tokens": 3, "total_cost": 0.02}})
    assert _extract_provider_usage(msg) == {"input_tokens": 3, "total_cost": 0.02}


@dataclass
class _AiMessage:
    content: str
    type: str = "ai"
    usage_metadata: dict[str, int | float] | None = None


def test_parse_agent_result_uses_most_recent_message_with_usage() -> None:
    result = {
        "messages": [
            _AiMessage(
                content="analysis step",
                usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            ),
            _AiMessage(content='{"findings": []}'),
        ],
        "structured_response": None,
    }

    parsed = _parse_agent_result(result)

    assert parsed.provider_usage == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    }
