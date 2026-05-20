"""Unit tests: compute_diff_scope — pure incremental-review scope logic."""

from __future__ import annotations

import pytest

from openbot.dispatcher.incremental import compute_diff_scope


def _pr_raw(*, head_sha: str, base_sha: str, before: str | None = None) -> dict:
    p: dict = {"pull_request": {"head": {"sha": head_sha}, "base": {"sha": base_sha}}}
    if before is not None:
        p["before"] = before
    return p


def test_first_review_no_last_sha() -> None:
    scope = compute_diff_scope(_pr_raw(head_sha="H1", base_sha="B1"), last_reviewed_sha=None)
    assert scope.head_sha == "H1"
    assert scope.base_sha == "B1"
    assert scope.is_incremental is False
    assert scope.is_force_push is False
    assert scope.last_reviewed_sha is None


def test_incremental_normal_push() -> None:
    """before_sha == last_reviewed_sha → incremental diff from last_reviewed to head."""
    scope = compute_diff_scope(
        _pr_raw(head_sha="H2", base_sha="B1", before="PREV1"),
        last_reviewed_sha="PREV1",
    )
    assert scope.base_sha == "PREV1"  # diff from last review point
    assert scope.head_sha == "H2"
    assert scope.is_incremental is True
    assert scope.is_force_push is False


def test_force_push_before_differs() -> None:
    """before_sha ≠ last_reviewed_sha → force push, full diff from PR base."""
    scope = compute_diff_scope(
        _pr_raw(head_sha="H3", base_sha="B1", before="OTHER"),
        last_reviewed_sha="PREV1",
    )
    assert scope.base_sha == "B1"
    assert scope.is_incremental is False
    assert scope.is_force_push is True


def test_missing_before_with_last_reviewed_is_force_push() -> None:
    """No 'before' key + last_reviewed_sha present → conservative force push."""
    scope = compute_diff_scope(_pr_raw(head_sha="H4", base_sha="B1"), last_reviewed_sha="PREV1")
    assert scope.is_force_push is True
    assert scope.is_incremental is False


def test_non_pr_payload_returns_empty_scope() -> None:
    scope = compute_diff_scope({}, last_reviewed_sha=None)
    assert scope.head_sha is None
    assert scope.base_sha is None
    assert scope.is_incremental is False
    assert scope.is_force_push is False


def test_diff_scope_is_frozen() -> None:
    scope = compute_diff_scope({}, last_reviewed_sha=None)
    with pytest.raises(AttributeError):
        scope.head_sha = "mutated"  # type: ignore[misc]
