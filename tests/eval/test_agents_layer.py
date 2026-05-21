"""Contract tests for the eval agent layer.

The solvers should use preconfigured agents from ``evals.agents`` rather
than assembling deepagents directly.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest


@pytest.mark.parametrize(
    ("module_name", "builder_name"),
    [
        ("evals.agents.review", "build_review_agent"),
        ("evals.agents.fix", "build_fix_agent"),
        ("evals.agents.test_generation", "build_test_generation_agent"),
        ("evals.agents.chat", "build_chat_agent"),
    ],
)
def test_task_agents_delegate_to_baseline_agent(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    builder_name: str,
) -> None:
    module = importlib.import_module(module_name)
    calls: list[dict[str, Any]] = []
    sentinel = object()

    def _fake_baseline(**kwargs: Any) -> object:
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(module, "build_baseline_agent", _fake_baseline)

    result = getattr(module, builder_name)(model="anthropic:test", backend="backend")

    assert result is sentinel
    assert len(calls) == 1
    assert calls[0]["model"] == "anthropic:test"
    assert calls[0]["backend"] == "backend"
    assert isinstance(calls[0]["system_prompt"], str)
    assert calls[0]["system_prompt"].strip()


def test_solvers_do_not_construct_deepagents_directly() -> None:
    solvers_dir = Path("evals/solvers")
    for solver_path in solvers_dir.glob("*.py"):
        source = solver_path.read_text(encoding="utf-8")
        assert "create_deep_agent" not in source, solver_path
        assert "build_baseline_agent" not in source, solver_path


def test_agent_langsmith_surface_is_canonical() -> None:
    langsmith = importlib.import_module("evals.inspect.langsmith")

    assert hasattr(langsmith, "configure_tracing_for_dataset")
    assert hasattr(langsmith, "LangSmithExperiment")
    assert not Path("evals/common/langsmith.py").exists()
    assert not Path("evals/common/langsmith_experiments.py").exists()
    assert not Path("evals/common/langsmith_feedback.py").exists()
    assert not Path("evals/agents/langsmith.py").exists()
    assert not Path("evals/agents/langsmith_feedback.py").exists()
