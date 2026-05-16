"""Usage extraction tests for the deepagents baseline provider."""

from __future__ import annotations

from dataclasses import dataclass

from evals.solvers.review import _extract_provider_usage


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
