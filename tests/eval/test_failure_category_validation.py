"""Unit tests for the PRD §12.4 failure_category enum check — E2-T16."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from evals.common._metadata_spec import ALLOWED_FAILURE_CATEGORIES


def _load_validator():
    """Load the script as a module so we can call `validate_failure_category` directly."""
    script = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "scripts"
        / "validate_langsmith_run.py"
    )
    spec = importlib.util.spec_from_file_location("validate_langsmith_run", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_enum_values_accepted() -> None:
    """Every PRD §12.4 enum value must pass validation."""
    mod = _load_validator()
    for cat in ALLOWED_FAILURE_CATEGORIES:
        mod.validate_failure_category(cat)  # no raise


def test_free_text_rejected() -> None:
    """Free-text failure_category must raise ValueError."""
    mod = _load_validator()
    for bad in ["random_bug", "transient_anything", "TIMEOUT", "Setup_Failure", ""]:
        with pytest.raises(ValueError, match="not in the PRD"):
            mod.validate_failure_category(bad)


def test_case_sensitivity() -> None:
    """Enum values are lowercase; uppercase variants must reject."""
    mod = _load_validator()
    with pytest.raises(ValueError):
        mod.validate_failure_category("BUDGET_STOP")
    with pytest.raises(ValueError):
        mod.validate_failure_category("Timeout")


def test_error_message_lists_allowed() -> None:
    """The ValueError message must enumerate the allowed enum (so CI failures are self-explanatory)."""
    mod = _load_validator()
    try:
        mod.validate_failure_category("foo")
    except ValueError as e:
        msg = str(e)
        assert "budget_stop" in msg  # spot-check a representative enum member
        assert "PRD" in msg


def test_help_text_lists_failure_categories() -> None:
    """`--help` should expose the enum so reviewers know the contract."""
    mod = _load_validator()
    help_text = mod._format_checklist()
    assert "failure_category" in help_text
    assert "12.4" in help_text
    # spot-check
    assert "budget_stop" in help_text or "'budget_stop'" in help_text
