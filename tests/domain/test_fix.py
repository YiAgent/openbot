"""Domain dataclasses for the fix workflow."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from openbot.domain.fix import FixAttempt, FixOutcome


def _attempt(**overrides: object) -> FixAttempt:
    defaults: dict[str, object] = {
        "summary": "fix the off-by-one in pagination",
        "files_changed": ("src/api/list.py",),
        "tests_passed": True,
        "test_command": "pytest tests/",
        "test_output": "3 passed",
        "diff": "diff --git a/src/api/list.py b/src/api/list.py\n",
    }
    defaults.update(overrides)
    return FixAttempt(**defaults)  # type: ignore[arg-type]


def test_attempt_holds_required_fields() -> None:
    a = _attempt()
    assert a.summary == "fix the off-by-one in pagination"
    assert a.files_changed == ("src/api/list.py",)
    assert a.tests_passed is True
    assert a.test_command == "pytest tests/"


def test_attempt_is_frozen() -> None:
    a = _attempt()
    with pytest.raises(FrozenInstanceError):
        a.summary = "no"  # type: ignore[misc]


def test_attempt_files_changed_is_tuple() -> None:
    # Lists would let callers mutate the value after construction.
    a = _attempt(files_changed=("a.py", "b.py"))
    assert isinstance(a.files_changed, tuple)


def test_outcome_holds_attempt_and_optional_pr_url() -> None:
    o = FixOutcome(attempt=_attempt(), pr_url="https://github.com/o/r/pull/9")
    assert o.attempt.tests_passed is True
    assert o.pr_url == "https://github.com/o/r/pull/9"
    assert o.error is None


def test_outcome_defaults_pr_url_and_error_to_none() -> None:
    o = FixOutcome(attempt=_attempt())
    assert o.pr_url is None
    assert o.error is None


def test_outcome_is_frozen() -> None:
    o = FixOutcome(attempt=_attempt())
    with pytest.raises(FrozenInstanceError):
        o.pr_url = "x"  # type: ignore[misc]


def test_outcome_can_record_failure_without_pr() -> None:
    # tests_passed=False is a legitimate terminal state — the use case
    # comments on the issue rather than opening a PR.
    failed = _attempt(tests_passed=False, test_output="1 failed")
    o = FixOutcome(attempt=failed, error=None)
    assert o.pr_url is None
    assert o.attempt.tests_passed is False


def test_outcome_can_record_error_after_passing_tests() -> None:
    # Tests passed but a downstream step (e.g. open_pull_request) raised
    # — error is set, pr_url is None.
    o = FixOutcome(
        attempt=_attempt(),
        pr_url=None,
        error="open_pull_request failed: 422",
    )
    assert o.error is not None
    assert o.pr_url is None
