"""Provider-usage aggregation tests.

These exercise :mod:`evals.common.usage`, which the four solvers share. The
file name is kept (instead of being renamed to e.g. ``test_usage.py``) so
git blame stays continuous — the previous incarnation of this file tested
the now-removed per-solver helper but covered exactly the same surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evals.common.usage import aggregate_provider_usage
from evals.solvers.review import _parse_agent_result


@dataclass
class _MessageWithUsage:
    content: str = "ai response"
    type: str = "ai"
    usage_metadata: dict[str, Any] | None = None
    response_metadata: dict[str, Any] = field(default_factory=dict)


def test_aggregate_returns_none_when_no_message_has_usage() -> None:
    msgs = [_MessageWithUsage(usage_metadata=None), _MessageWithUsage(usage_metadata=None)]
    assert aggregate_provider_usage(msgs) is None


def test_aggregate_sums_across_messages() -> None:
    msgs = [
        _MessageWithUsage(
            usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        ),
        _MessageWithUsage(
            usage_metadata={"input_tokens": 200, "output_tokens": 30, "total_tokens": 230}
        ),
        _MessageWithUsage(
            usage_metadata={"input_tokens": 50, "output_tokens": 5, "total_tokens": 55}
        ),
    ]
    assert aggregate_provider_usage(msgs) == {
        "input_tokens": 350,
        "output_tokens": 55,
        "total_tokens": 405,
    }


def test_aggregate_handles_openai_legacy_shape() -> None:
    # Some providers/older message versions surface OpenAI's prompt/completion
    # shape inside response_metadata.usage rather than usage_metadata.
    msg = _MessageWithUsage(
        usage_metadata=None,
        response_metadata={
            "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}
        },
    )
    assert aggregate_provider_usage([msg]) == {
        "input_tokens": 40,
        "output_tokens": 10,
        "total_tokens": 50,
    }


def test_aggregate_reconstructs_total_when_missing() -> None:
    msg = _MessageWithUsage(usage_metadata={"input_tokens": 7, "output_tokens": 3})
    assert aggregate_provider_usage([msg]) == {
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
    }


def test_aggregate_sums_token_details_per_key() -> None:
    msgs = [
        _MessageWithUsage(
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_token_details": {"cache_read": 80, "cache_creation": 0},
                "output_token_details": {"reasoning": 10},
            }
        ),
        _MessageWithUsage(
            usage_metadata={
                "input_tokens": 200,
                "output_tokens": 30,
                "total_tokens": 230,
                "input_token_details": {"cache_read": 150, "cache_creation": 25},
                "output_token_details": {"reasoning": 8},
            }
        ),
    ]
    result = aggregate_provider_usage(msgs)
    assert result is not None
    assert result["input_token_details"] == {"cache_read": 230, "cache_creation": 25}
    assert result["output_token_details"] == {"reasoning": 18}


def test_parse_agent_result_attaches_aggregate_usage() -> None:
    # Two LLM-call rounds: parsed.provider_usage must reflect the sum so the
    # experiment column matches LangSmith trace aggregation.
    result = {
        "messages": [
            _MessageWithUsage(
                content="analysis step",
                usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            ),
            _MessageWithUsage(
                content='{"findings": []}',
                usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            ),
        ],
        "structured_response": None,
    }
    parsed = _parse_agent_result(result)
    assert parsed.provider_usage == {
        "input_tokens": 16,
        "output_tokens": 9,
        "total_tokens": 25,
    }
