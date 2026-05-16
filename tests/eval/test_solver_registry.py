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


def test_review_task_metadata_records_solver_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The @task entrypoint stamps solver/judge identity into Task.metadata."""
    from inspect_ai.dataset import MemoryDataset, Sample

    from evals.tasks import review_martian as task_mod

    # Stub out LangSmith network calls — the test only cares about metadata.
    monkeypatch.setattr(
        task_mod,
        "langsmith_dataset",
        lambda _v: MemoryDataset(samples=[Sample(input="x", target="[]")], name="stub"),
    )
    monkeypatch.setattr(task_mod, "configure_tracing_for_dataset", lambda _v: {})

    task = task_mod.review_martian_baseline_crb()

    assert task.metadata["solver_id"] == "deepagents_baseline"
    assert task.metadata["solver_family"] == "baseline"
    assert task.metadata["dataset_version"] == "martian_2026w20"
    assert task.metadata["judge_label"] == "martian_crb_verbatim"
    assert task.metadata["judge_model_id"] == "claude-opus-4-5"
