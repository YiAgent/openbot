"""Contract tests for eval task construction and wiring (post-Phase-2 refactor)."""

from __future__ import annotations

from types import SimpleNamespace

from evals.tasks import review_martian


class _FakeTask:
    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.kwargs = kwargs
        self.solver = kwargs.get("solver")


def test_review_task_uses_openbot_solver(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """review_martian_baseline_crb must wire openbot_review_solver, solver_id=openbot_agent."""
    monkeypatch.setattr(review_martian, "configure_tracing_for_dataset", lambda _n: None)
    monkeypatch.setattr(review_martian, "langsmith_dataset", lambda _n: "dataset")
    monkeypatch.setattr(review_martian, "openbot_review_solver", lambda: "solver")
    monkeypatch.setattr(review_martian, "_build_overlap_scorer", lambda judge: ("scorer", judge))
    monkeypatch.setattr(review_martian, "Task", _FakeTask)

    task = review_martian.review_martian_baseline_crb()
    assert task.kwargs["solver"] == "solver"
    assert task.kwargs["metadata"]["solver_id"] == "openbot_agent"
    assert task.kwargs["metadata"]["solver_family"] == "openbot_agent"


def test_fix_task_wires_openbot_solver_and_exporter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """fix_swe_bench must wire openbot_fix_solver and produce an exporter scorer."""
    import sys

    sys.modules.pop("evals.tasks.fix_swe_bench", None)
    import importlib

    fix_mod = importlib.import_module("evals.tasks.fix_swe_bench")

    exp_calls: dict = {}

    def _fake_experiment(**kw):  # type: ignore[no-untyped-def]
        exp_calls.update(kw)
        return SimpleNamespace(scorer="fake-scorer", metadata={})

    monkeypatch.setattr(fix_mod, "configure_tracing_for_dataset", lambda _n: None)
    monkeypatch.setattr(fix_mod, "git_sha", lambda: "gitsha")
    monkeypatch.setattr(fix_mod, "resolve_model_label", lambda: "model-x")
    monkeypatch.setattr(fix_mod, "build_export_experiment", _fake_experiment)
    monkeypatch.setattr(fix_mod, "openbot_fix_solver", lambda: "fix-solver")
    monkeypatch.setattr(fix_mod, "load_issue_dataset", lambda **_: [])
    monkeypatch.setattr(fix_mod, "Task", _FakeTask)

    task = fix_mod.fix_swe_bench()

    assert exp_calls["dataset_version"] == "fix_swe_bench_verified"
    assert exp_calls["schema"].__name__ == "SweBenchPrediction"
    assert task.kwargs["scorer"] == "fake-scorer"
    assert task.kwargs["solver"] == "fix-solver"
    assert task.kwargs["metadata"]["solver_family"] == "openbot_agent"


def test_swt_task_wires_openbot_solver_and_exporter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """test_swt_bench must wire openbot_test_generation_solver and produce an exporter scorer."""
    import sys

    sys.modules.pop("evals.tasks.test_swt_bench", None)
    import importlib

    swt_mod = importlib.import_module("evals.tasks.test_swt_bench")

    exp_calls: dict = {}

    def _fake_experiment(**kw):  # type: ignore[no-untyped-def]
        exp_calls.update(kw)
        return SimpleNamespace(scorer="fake-scorer", metadata={})

    monkeypatch.setattr(swt_mod, "configure_tracing_for_dataset", lambda _n: None)
    monkeypatch.setattr(swt_mod, "git_sha", lambda: "gitsha")
    monkeypatch.setattr(swt_mod, "resolve_model_label", lambda: "model-x")
    monkeypatch.setattr(swt_mod, "build_export_experiment", _fake_experiment)
    monkeypatch.setattr(swt_mod, "openbot_test_generation_solver", lambda: "swt-solver")
    monkeypatch.setattr(swt_mod, "load_issue_dataset", lambda **_: [])
    monkeypatch.setattr(swt_mod, "Task", _FakeTask)

    task = swt_mod.test_swt_bench()

    assert exp_calls["dataset_version"] == "test_swt_bench_verified"
    assert exp_calls["schema"].__name__ == "SwtBenchPrediction"
    assert task.kwargs["scorer"] == "fake-scorer"
    assert task.kwargs["solver"] == "swt-solver"
    assert task.kwargs["metadata"]["solver_family"] == "openbot_agent"
