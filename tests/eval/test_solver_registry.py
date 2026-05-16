"""Review solver-provider tests for apples-to-apples eval comparisons."""

from __future__ import annotations

import pytest


def test_deepagents_baseline_provider_is_registered() -> None:
    from evals.solvers.registry import get_review_solver

    solver_factory = get_review_solver("deepagents_baseline")
    assert callable(solver_factory)


def test_openbot_prod_provider_is_reserved_but_not_implemented() -> None:
    from evals.solvers.registry import get_review_solver

    with pytest.raises(NotImplementedError, match="openbot_prod"):
        get_review_solver("openbot_prod")


def test_unknown_solver_id_raises_clear_error() -> None:
    from evals.solvers.registry import get_review_solver

    with pytest.raises(ValueError, match="Unknown review solver_id"):
        get_review_solver("mystery_solver")


def test_review_task_metadata_records_solver_identity() -> None:
    from evals.tasks.review_martian import build_review_martian_task

    task = build_review_martian_task(solver_id="deepagents_baseline")

    assert task.metadata["solver_id"] == "deepagents_baseline"
    assert task.metadata["solver_family"] == "baseline"
