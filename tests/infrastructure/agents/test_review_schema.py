"""Tests for the pydantic ⇄ domain boundary at infrastructure/agents/_review_schema.py."""

from __future__ import annotations

import pytest

from openbot.domain.review import Finding, ReviewFindings
from openbot.infrastructure.agents._review_schema import (
    FindingSchema,
    ReviewFindingsSchema,
    parse_structured_response,
)


def test_to_domain_passes_well_formed_findings() -> None:
    schema = ReviewFindingsSchema(
        summary="2 issues",
        findings=[
            FindingSchema(severity="high", file="x.py", message="bug", line=10, quote="x = 1"),
            FindingSchema(severity="nit", file="y.py", message="style"),
        ],
    )

    result = schema.to_domain()

    assert result == ReviewFindings(
        summary="2 issues",
        findings=(
            Finding(severity="high", file="x.py", message="bug", line=10, quote="x = 1"),
            Finding(severity="nit", file="y.py", message="style"),
        ),
    )


def test_to_domain_drops_unknown_severity() -> None:
    """Anti-corruption layer: one bad finding shouldn't fail the whole review."""
    schema = ReviewFindingsSchema(
        summary="mixed",
        findings=[
            FindingSchema(severity="catastrophic", file="x.py", message="bad"),
            FindingSchema(severity="medium", file="y.py", message="ok"),
        ],
    )

    result = schema.to_domain()

    assert len(result.findings) == 1
    assert result.findings[0].severity == "medium"


def test_to_domain_preserves_empty_findings() -> None:
    schema = ReviewFindingsSchema(summary="LGTM")
    assert schema.to_domain() == ReviewFindings(summary="LGTM", findings=())


def test_parse_structured_response_accepts_pydantic_instance() -> None:
    schema = ReviewFindingsSchema(summary="LGTM")
    assert parse_structured_response(schema) == ReviewFindings(summary="LGTM", findings=())


def test_parse_structured_response_accepts_plain_dict() -> None:
    payload = {
        "summary": "1 issue",
        "findings": [{"severity": "critical", "file": "x.py", "message": "leak", "line": 1}],
    }
    result = parse_structured_response(payload)
    assert result.summary == "1 issue"
    assert result.findings == (Finding(severity="critical", file="x.py", message="leak", line=1),)


def test_parse_structured_response_rejects_garbage() -> None:
    """Silent approve on bad input would mask a broken responder. Fail loud."""
    with pytest.raises(ValueError, match="deepagents_structured_response_unexpected_type"):
        parse_structured_response("not a model")  # type: ignore[arg-type]


def test_findings_schema_forbids_extra_fields() -> None:
    """If the LLM invents a `rule_id` we don't yet handle, fail explicitly so
    a future field-add is a deliberate code change rather than a silent
    swallow."""
    with pytest.raises(ValueError):
        FindingSchema.model_validate(
            {"severity": "low", "file": "x.py", "message": "m", "rule_id": "X100"}
        )
