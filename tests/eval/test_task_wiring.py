"""Contract tests for eval task construction and wiring (post-Modal refactor)."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

from evals.tasks import chat_swe_qa_pro, review_martian


class _FakeTask:
    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.kwargs = kwargs
        self.solver = kwargs.get("solver")


def test_chat_openbot_task_wires_agent_solver_without_inspect_sandbox(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """+Agent variant must NOT declare a task-level Inspect Docker sandbox.

    Sandbox lifecycle is owned by the solver (Modal) now, so the Task
    surface should not carry a ``sandbox=`` field. Asserting it's absent
    prevents accidental regression of the architectural boundary.
    """
    calls: dict[str, object] = {}

    def _dataset(name, *, converter):  # type: ignore[no-untyped-def]
        calls["dataset"] = (name, converter)
        return "dataset"

    monkeypatch.setattr(
        chat_swe_qa_pro,
        "configure_tracing_for_dataset",
        lambda name: calls.setdefault("trace", name),
    )
    monkeypatch.setattr(chat_swe_qa_pro, "langsmith_dataset", _dataset)
    monkeypatch.setattr(chat_swe_qa_pro, "deepagents_agent_swe_qa_solver", lambda: "agent-solver")
    monkeypatch.setattr(chat_swe_qa_pro, "swe_qa_pro_judge_scorer", lambda: "scorer")
    monkeypatch.setattr(chat_swe_qa_pro, "Task", _FakeTask)

    task = chat_swe_qa_pro.chat_swe_qa_pro_openbot()

    assert calls["dataset"] == ("chat_swe_qa_pro_v1", chat_swe_qa_pro.qa_example_to_agent_sample)
    assert task.kwargs["solver"] == "agent-solver"
    assert "sandbox" not in task.kwargs, "Task must not declare Inspect sandbox post-refactor"
    assert task.kwargs["metadata"]["solver_family"] == "deepagents_agent"


def test_review_task_metadata_unchanged(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(review_martian, "configure_tracing_for_dataset", lambda _n: None)
    monkeypatch.setattr(review_martian, "langsmith_dataset", lambda _n: "dataset")
    monkeypatch.setattr(review_martian, "deepagents_baseline_review_solver", lambda: "solver")
    monkeypatch.setattr(review_martian, "_build_overlap_scorer", lambda judge: ("scorer", judge))
    monkeypatch.setattr(review_martian, "Task", _FakeTask)

    task = review_martian.review_martian_baseline_crb()
    assert task.kwargs["solver"] == "solver"
    assert task.kwargs["metadata"]["solver_family"] == "deepagents_baseline"


class _FakeExperiment:
    def wrap(self, scorer, **kwargs):  # type: ignore[no-untyped-def]
        return ("wrapped", scorer, kwargs)

    def metadata(self) -> dict[str, str]:
        return {"langsmith_experiment_name": "exp"}


def _import_coding_task_modules(monkeypatch):  # type: ignore[no-untyped-def]
    """Reload SWE/SWT task modules with HF dataset loading stubbed out.

    The real modules call ``datasets.load_dataset`` at task-construction
    time, which would hit HuggingFace; we replace it with a tiny fake
    iterable so the wiring tests stay offline.
    """
    fake_datasets = ModuleType("datasets")

    def _load_dataset(_name, *, split):  # type: ignore[no-untyped-def]
        return [
            {
                "instance_id": "x__y-1",
                "repo": "x/y",
                "base_commit": "sha",
                "problem_statement": "issue body",
                "version": "1.0",
            }
        ]

    fake_datasets.load_dataset = _load_dataset  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "datasets", fake_datasets)
    sys.modules.pop("evals.tasks.fix_swe_bench_verified", None)
    sys.modules.pop("evals.tasks.test_swt_bench_verified", None)
    return (
        importlib.import_module("evals.tasks.fix_swe_bench_verified"),
        importlib.import_module("evals.tasks.test_swt_bench_verified"),
    )


def test_fix_task_wires_exporter_and_modal_solver(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fix_mod, _ = _import_coding_task_modules(monkeypatch)

    monkeypatch.setattr(fix_mod, "configure_tracing_for_dataset", lambda _n: None)
    monkeypatch.setattr(fix_mod, "_git_sha", lambda: "gitsha")
    monkeypatch.setattr(fix_mod, "_resolve_model_label", lambda: "model-x")
    monkeypatch.setattr(
        fix_mod.LangSmithExperiment, "start", classmethod(lambda *_a, **_kw: _FakeExperiment())
    )
    monkeypatch.setattr(fix_mod, "deepagents_baseline_swe_solver", lambda: "deep-solver")
    monkeypatch.setattr(fix_mod, "prediction_exporter", lambda **kw: ("exporter", kw))
    # Inspect's Task validates the scorer at construction time and rejects
    # non-registered scorer objects; swap in a recording fake.
    monkeypatch.setattr(fix_mod, "Task", _FakeTask)

    task = fix_mod.fix_swe_bench_verified_deepagents()

    wrapped = task.kwargs["scorer"]
    assert wrapped[0] == "wrapped"
    inner = wrapped[1]
    assert inner[0] == "exporter"
    assert inner[1]["dataset_version"] == "fix_swe_bench_verified"
    assert inner[1]["schema"].__name__ == "SweBenchPrediction"
    assert task.kwargs["solver"] == "deep-solver"


def test_swt_task_wires_exporter_and_modal_solver(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, swt_mod = _import_coding_task_modules(monkeypatch)

    monkeypatch.setattr(swt_mod, "configure_tracing_for_dataset", lambda _n: None)
    monkeypatch.setattr(swt_mod, "_git_sha", lambda: "gitsha")
    monkeypatch.setattr(swt_mod, "_resolve_model_label", lambda: "model-x")
    monkeypatch.setattr(
        swt_mod.LangSmithExperiment, "start", classmethod(lambda *_a, **_kw: _FakeExperiment())
    )
    monkeypatch.setattr(swt_mod, "deepagents_baseline_swt_solver", lambda: "deep-solver")
    monkeypatch.setattr(swt_mod, "prediction_exporter", lambda **kw: ("exporter", kw))
    monkeypatch.setattr(swt_mod, "Task", _FakeTask)

    task = swt_mod.test_swt_bench_verified_deepagents()
    wrapped = task.kwargs["scorer"]
    assert wrapped[0] == "wrapped"
    inner = wrapped[1]
    assert inner[1]["dataset_version"] == "test_swt_bench_verified"
    assert inner[1]["schema"].__name__ == "SwtBenchPrediction"
    assert task.kwargs["solver"] == "deep-solver"


def test_coding_task_model_labels_use_shared_deepagents_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fix_mod, swt_mod = _import_coding_task_modules(monkeypatch)

    monkeypatch.delenv("INSPECT_EVAL_MODEL", raising=False)
    monkeypatch.delenv("OPENBOT_FIX_MODEL_ID", raising=False)
    monkeypatch.delenv("OPENBOT_TEST_MODEL_ID", raising=False)
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_MODEL", "mimo-v2.5")

    assert fix_mod._resolve_model_label() == "anthropic:mimo-v2.5"
    assert swt_mod._resolve_model_label() == "anthropic:mimo-v2.5"
