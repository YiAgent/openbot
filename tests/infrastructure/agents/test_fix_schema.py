"""Schema bridge tests — pydantic ⇄ domain for the fix responder."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openbot.domain.fix import FixAttempt, FixOutcome
from openbot.infrastructure.agents._fix_schema import (
    FixOutcomeSchema,
    parse_structured_response,
)


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "attempt": {
            "summary": "fix off-by-one",
            "files_changed": ["src/api/list.py"],
            "tests_passed": True,
            "test_command": "pytest -q",
            "test_output": "3 passed in 0.1s",
            "diff": "diff --git a/src/api/list.py b/src/api/list.py\n",
        },
    }
    base.update(overrides)
    return base


def test_validates_minimal_payload() -> None:
    model = FixOutcomeSchema.model_validate(_payload())
    assert model.attempt.summary == "fix off-by-one"
    assert model.attempt.tests_passed is True


def test_to_domain_returns_fix_outcome() -> None:
    model = FixOutcomeSchema.model_validate(_payload())
    domain = model.to_domain()
    assert isinstance(domain, FixOutcome)
    assert isinstance(domain.attempt, FixAttempt)
    # Pydantic gives us a list — bridge must convert to tuple to satisfy
    # the frozen-dataclass field type.
    assert domain.attempt.files_changed == ("src/api/list.py",)
    assert domain.pr_url is None
    assert domain.error is None


def test_rejects_unknown_extras() -> None:
    # ``extra="forbid"`` keeps the agent from sneaking new keys past us.
    bad = _payload()
    bad["unexpected_field"] = "value"
    with pytest.raises(ValidationError):
        FixOutcomeSchema.model_validate(bad)


def test_parse_structured_response_accepts_dict() -> None:
    out = parse_structured_response(_payload())
    assert isinstance(out, FixOutcome)
    assert out.attempt.test_command == "pytest -q"


def test_parse_structured_response_accepts_pydantic_instance() -> None:
    model = FixOutcomeSchema.model_validate(_payload())
    out = parse_structured_response(model)
    assert isinstance(out, FixOutcome)


def test_parse_structured_response_raises_on_garbage() -> None:
    with pytest.raises(ValueError, match="deepagents_fix_structured_response_unexpected_type"):
        parse_structured_response(42)  # type: ignore[arg-type]


def test_handles_tests_failed_payload() -> None:
    p = _payload()
    p["attempt"] = {  # type: ignore[dict-item]
        **p["attempt"],  # type: ignore[dict-item]
        "tests_passed": False,
        "test_output": "1 failed",
    }
    out = parse_structured_response(p)
    assert out.attempt.tests_passed is False
    assert out.attempt.test_output == "1 failed"
