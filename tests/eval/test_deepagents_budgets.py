"""Contract tests for the global deepagents budget middleware stack."""

from __future__ import annotations

from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)

from evals.common.deepagents_baseline import (
    _DEFAULT_MODEL_CALL_LIMIT,
    _DEFAULT_RECURSION_LIMIT,
    _DEFAULT_TOOL_CALL_LIMIT,
    build_budget_middlewares,
    build_run_config,
    get_model_call_limit,
    get_recursion_limit,
    get_tool_call_limit,
    resolve_model,
)


def test_default_budgets_match_documented_baseline() -> None:
    assert get_model_call_limit() == _DEFAULT_MODEL_CALL_LIMIT
    assert get_tool_call_limit() == _DEFAULT_TOOL_CALL_LIMIT
    assert get_recursion_limit() == _DEFAULT_RECURSION_LIMIT


def test_env_var_override_takes_effect(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_MODEL_CALL_LIMIT", "17")
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_TOOL_CALL_LIMIT", "23")
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_RECURSION_LIMIT", "42")
    assert get_model_call_limit() == 17
    assert get_tool_call_limit() == 23
    assert get_recursion_limit() == 42


def test_invalid_env_var_falls_back_to_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_MODEL_CALL_LIMIT", "not-a-number")
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_TOOL_CALL_LIMIT", "-5")
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_RECURSION_LIMIT", "0")
    assert get_model_call_limit() == _DEFAULT_MODEL_CALL_LIMIT
    assert get_tool_call_limit() == _DEFAULT_TOOL_CALL_LIMIT
    assert get_recursion_limit() == _DEFAULT_RECURSION_LIMIT


def test_build_budget_middlewares_default_exit_behavior() -> None:
    """Tool budget must exit_behavior='continue' so the model gets a final turn."""
    mws = build_budget_middlewares()
    assert len(mws) == 2
    by_type = {type(m).__name__: m for m in mws}
    assert "ToolCallLimitMiddleware" in by_type
    assert "ModelCallLimitMiddleware" in by_type

    tool_mw = by_type["ToolCallLimitMiddleware"]
    assert isinstance(tool_mw, ToolCallLimitMiddleware)
    assert tool_mw.exit_behavior == "continue"
    assert tool_mw.thread_limit == _DEFAULT_TOOL_CALL_LIMIT

    model_mw = by_type["ModelCallLimitMiddleware"]
    assert isinstance(model_mw, ModelCallLimitMiddleware)
    assert model_mw.exit_behavior == "end"
    assert model_mw.thread_limit == _DEFAULT_MODEL_CALL_LIMIT


def test_build_budget_middlewares_explicit_overrides() -> None:
    mws = build_budget_middlewares(model_call_limit=7, tool_call_limit=11)
    by_type = {type(m).__name__: m for m in mws}
    assert by_type["ModelCallLimitMiddleware"].thread_limit == 7
    assert by_type["ToolCallLimitMiddleware"].thread_limit == 11


def test_build_run_config_injects_recursion_limit() -> None:
    cfg = build_run_config(
        sample_id="s1",
        dataset_version="ds",
        solver_family="deepagents_baseline",
        model="anthropic:claude-sonnet-4-6",
    )
    assert cfg["recursion_limit"] == _DEFAULT_RECURSION_LIMIT
    assert cfg["run_name"] == "ds/s1"
    assert cfg["metadata"]["solver_family"] == "deepagents_baseline"


def test_build_run_config_explicit_recursion_limit_wins() -> None:
    cfg = build_run_config(
        sample_id="s1",
        dataset_version="ds",
        solver_family="sf",
        model="m",
        recursion_limit=12,
    )
    assert cfg["recursion_limit"] == 12


def test_resolve_model_uses_shared_deepagents_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_MODEL", "mimo-v2.5")
    assert resolve_model() == "anthropic:mimo-v2.5"


def test_resolve_model_treats_empty_shared_env_as_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_MODEL", "")
    assert resolve_model(fallback="anthropic:claude-opus-4-7") == "anthropic:claude-opus-4-7"


def test_resolve_model_explicit_override_beats_shared_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_MODEL", "mimo-v2.5")
    assert resolve_model(override="openai:gpt-5") == "openai:gpt-5"
