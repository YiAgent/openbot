"""Contract tests for eval task construction and wiring (post-data-unification refactor)."""

from __future__ import annotations

from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, mean, scorer


def _noop_scorer():  # type: ignore[no-untyped-def]
    """Minimal real Inspect scorer for task construction."""

    @scorer(metrics=[mean()])
    def _make():  # type: ignore[no-untyped-def]
        async def _score(state, target):  # type: ignore[no-untyped-def]
            return Score(value=0)

        return _score

    return _make()


_FAKE_DATASET = MemoryDataset(samples=[Sample(id="x", input="y")], name="fake")


def test_review_task_delegates_to_suite(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """review_martian_openbot must delegate to REVIEW.build_task with a scorer and extra_metadata."""
    from evals.tasks import review_martian

    captured: dict = {}

    def _fake_build_task(*, solver, scorer=None, extra_metadata=None):  # type: ignore[no-untyped-def]
        captured["solver"] = solver
        captured["scorer"] = scorer
        captured["extra_metadata"] = extra_metadata
        from inspect_ai import Task

        return Task(dataset=_FAKE_DATASET, solver=solver, scorer=_noop_scorer())

    monkeypatch.setattr(review_martian.REVIEW, "build_task", _fake_build_task)
    monkeypatch.setattr(review_martian, "openbot_review_solver", lambda: "solver")
    monkeypatch.setattr(review_martian, "make_review_overlap_scorer", lambda: "scorer")

    review_martian.review_martian_openbot()

    assert captured["solver"] == "solver"
    assert captured["scorer"] == "scorer"
    extra = captured["extra_metadata"] or {}
    assert extra.get("judge_label") == "martian_crb_verbatim"


def test_fix_task_delegates_to_suite(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """fix_swe_bench must delegate to FIX.build_task."""
    import importlib
    import sys

    sys.modules.pop("evals.tasks.fix_swe_bench", None)
    fix_mod = importlib.import_module("evals.tasks.fix_swe_bench")

    called_with: dict = {}

    def _fake_build_task(*, solver, scorer=None, extra_metadata=None):  # type: ignore[no-untyped-def]
        called_with["solver"] = solver
        from inspect_ai import Task

        return Task(dataset=_FAKE_DATASET, solver=solver, scorer=_noop_scorer())

    monkeypatch.setattr(fix_mod.FIX, "build_task", _fake_build_task)
    monkeypatch.setattr(fix_mod, "openbot_fix_solver", lambda: "fix-solver")

    fix_mod.fix_swe_bench()
    assert called_with["solver"] == "fix-solver"


def test_swt_task_delegates_to_suite(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """test_swt_bench must delegate to SWT.build_task."""
    import importlib
    import sys

    sys.modules.pop("evals.tasks.test_swt_bench", None)
    swt_mod = importlib.import_module("evals.tasks.test_swt_bench")

    called_with: dict = {}

    def _fake_build_task(*, solver, scorer=None, extra_metadata=None):  # type: ignore[no-untyped-def]
        called_with["solver"] = solver
        from inspect_ai import Task

        return Task(dataset=_FAKE_DATASET, solver=solver, scorer=_noop_scorer())

    monkeypatch.setattr(swt_mod.SWT, "build_task", _fake_build_task)
    monkeypatch.setattr(swt_mod, "openbot_test_generation_solver", lambda: "swt-solver")

    swt_mod.test_swt_bench()
    assert called_with["solver"] == "swt-solver"
