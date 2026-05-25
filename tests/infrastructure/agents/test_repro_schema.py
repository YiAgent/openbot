"""Pydantic boundary for the reproduce responder.

Mirrors ``tests/infrastructure/agents/test_fix_schema.py``: schema
accepts pydantic instances and dicts, rejects unexpected types and
missing fields, and trims ``output_excerpt`` to the 2000-char budget
that the downstream renderer + audit row depend on.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from openbot.domain.repro import ReproOutcome, ReproStatus
from openbot.infrastructure.agents._repro_schema import (
    ReproOutcomeSchema,
    parse_structured_response,
)


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "reproduced",
        "summary": "Confirmed: list index errors on empty input.",
        "command": "pytest tests/test_pagination.py::test_empty -q",
        "exit_code": 1,
        "output_excerpt": "IndexError: list index out of range",
        "hypothesis": "src/api/list.py:42 dereferences items[0] without a guard.",
        "repro_artifact": "#!/usr/bin/env bash\npytest -q\n",
        "repro_artifact_filename": "repro_reproduced.sh",
    }
    base.update(overrides)
    return base


# ── happy path ───────────────────────────────────────────────────────────────


def test_parse_dict_returns_domain_outcome() -> None:
    outcome = parse_structured_response(_valid_payload())
    assert isinstance(outcome, ReproOutcome)
    assert outcome.status is ReproStatus.REPRODUCED
    assert outcome.command == "pytest tests/test_pagination.py::test_empty -q"
    assert outcome.exit_code == 1


def test_parse_pydantic_instance_returns_domain_outcome() -> None:
    model = ReproOutcomeSchema(**_valid_payload())
    outcome = parse_structured_response(model)
    assert isinstance(outcome, ReproOutcome)
    assert outcome.status is ReproStatus.REPRODUCED


def test_all_status_values_are_accepted() -> None:
    for status in ("reproduced", "not_reproduced", "insufficient_info", "agent_failed"):
        payload = _valid_payload(
            status=status,
            # INSUFFICIENT_INFO / AGENT_FAILED naturally have no command — the
            # schema does NOT enforce that pairing (we pin it in tests, not
            # constructors — see domain/repro.py docstring). We just check
            # the status string is accepted as a valid value.
        )
        outcome = parse_structured_response(payload)
        assert outcome.status.value == status


# ── error paths ──────────────────────────────────────────────────────────────


def test_missing_required_field_raises_validation_error() -> None:
    payload = _valid_payload()
    del payload["status"]
    with pytest.raises(ValidationError):
        parse_structured_response(payload)


def test_unknown_status_value_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        parse_structured_response(_valid_payload(status="kinda-reproduced"))


def test_extra_fields_rejected() -> None:
    # Anti-corruption layer: a runaway prompt cannot sneak free-form keys
    # past the schema.
    with pytest.raises(ValidationError):
        parse_structured_response(_valid_payload(confidence=0.9))


def test_unexpected_raw_type_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_structured_response("not a dict and not a model")  # type: ignore[arg-type]


# ── truncation budget ────────────────────────────────────────────────────────


def test_output_excerpt_under_budget_is_unchanged() -> None:
    payload = _valid_payload(output_excerpt="x" * 1500)
    outcome = parse_structured_response(payload)
    assert outcome.output_excerpt == "x" * 1500


def test_output_excerpt_over_budget_is_truncated_with_marker() -> None:
    payload = _valid_payload(output_excerpt="x" * 5000)
    outcome = parse_structured_response(payload)
    assert len(outcome.output_excerpt) <= 2000
    assert outcome.output_excerpt.endswith("…[truncated]")
