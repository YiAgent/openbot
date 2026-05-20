"""Contract tests for the global deepagents budget middleware stack."""

from __future__ import annotations

import pytest
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from pydantic import ValidationError

from evals.agents.baseline import (
    build_budget_middlewares,
    build_run_config,
    get_model_call_limit,
    get_recursion_limit,
    get_tool_call_limit,
    resolve_model,
)
from evals.common import config

# Documented baseline — mirrors the pydantic Field defaults in
# :class:`evals.common.config.DeepAgentsSettings`. Hardcoded here so the
# tests catch drift if someone bumps the default without thinking.
_BASELINE_MODEL_CALL_LIMIT = 100
_BASELINE_TOOL_CALL_LIMIT = 200
_BASELINE_RECURSION_LIMIT = 400


def test_default_budgets_match_documented_baseline() -> None:
    assert get_model_call_limit() == _BASELINE_MODEL_CALL_LIMIT
    assert get_tool_call_limit() == _BASELINE_TOOL_CALL_LIMIT
    assert get_recursion_limit() == _BASELINE_RECURSION_LIMIT


def test_env_var_override_takes_effect(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(config.DEEPAGENTS_MODEL_CALL_LIMIT_ENV, "17")
    monkeypatch.setenv(config.DEEPAGENTS_TOOL_CALL_LIMIT_ENV, "23")
    monkeypatch.setenv(config.DEEPAGENTS_RECURSION_LIMIT_ENV, "42")
    config.get_eval_config.cache_clear()
    assert get_model_call_limit() == 17
    assert get_tool_call_limit() == 23
    assert get_recursion_limit() == 42


def test_invalid_env_var_fails_loud_at_load(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Pydantic ``PositiveInt`` rejects bad env values at load time.

    Prior behavior silently fell back to defaults — that hid misconfigured
    Doppler entries. Strict validation surfaces them immediately.
    """
    monkeypatch.setenv(config.DEEPAGENTS_MODEL_CALL_LIMIT_ENV, "not-a-number")
    config.get_eval_config.cache_clear()
    with pytest.raises(ValidationError):
        get_model_call_limit()


def test_build_budget_middlewares_default_exit_behavior() -> None:
    """Tool budget must exit_behavior='continue' so the model gets a final turn.

    The stack now includes ``ToolCallRepetitionGuard`` and
    ``ForceCommitBeforeBudget`` ahead of the call-limit middlewares —
    they're harness-level convergence guards that work for any model.
    See :mod:`evals.agents.convergence_middleware`.
    """
    mws = build_budget_middlewares()
    by_type = {type(m).__name__: m for m in mws}
    # All four guards present.
    assert {"ToolCallRepetitionGuard", "ForceCommitBeforeBudget"} <= by_type.keys()
    assert "ToolCallLimitMiddleware" in by_type
    assert "ModelCallLimitMiddleware" in by_type

    tool_mw = by_type["ToolCallLimitMiddleware"]
    assert isinstance(tool_mw, ToolCallLimitMiddleware)
    assert tool_mw.exit_behavior == "continue"
    assert tool_mw.thread_limit == _BASELINE_TOOL_CALL_LIMIT

    model_mw = by_type["ModelCallLimitMiddleware"]
    assert isinstance(model_mw, ModelCallLimitMiddleware)
    assert model_mw.exit_behavior == "end"
    assert model_mw.thread_limit == _BASELINE_MODEL_CALL_LIMIT


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
    assert cfg["recursion_limit"] == _BASELINE_RECURSION_LIMIT
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
    monkeypatch.setenv(config.DEEPAGENTS_MODEL_ENV, "mimo-v2.5")
    assert resolve_model() == "anthropic:mimo-v2.5"


def test_resolve_model_treats_empty_shared_env_as_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(config.DEEPAGENTS_MODEL_ENV, "")
    assert resolve_model(fallback="anthropic:claude-opus-4-7") == "anthropic:claude-opus-4-7"


def test_resolve_model_explicit_override_beats_shared_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(config.DEEPAGENTS_MODEL_ENV, "mimo-v2.5")
    assert resolve_model(override="openai:gpt-5") == "openai:gpt-5"


def test_display_model_name_strips_langchain_prefix() -> None:
    """LangSmith / dashboard surfaces want the bare model id, no routing prefix."""
    from evals.agents.baseline import display_model_name

    # Common case: provider:model → strip provider
    assert display_model_name("anthropic:glm-4.5-air") == "glm-4.5-air"
    assert display_model_name("openai:gpt-5") == "gpt-5"
    # No prefix → unchanged (idempotent)
    assert display_model_name("glm-4.5-air") == "glm-4.5-air"
    # Multi-segment ids strip only the leading provider segment
    assert display_model_name("openai:org-abc:gpt-5") == "org-abc:gpt-5"


def test_thinking_budget_maps_levels(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Thinking-level string maps to Anthropic ``budget_tokens`` via config."""
    from evals.agents.baseline import (
        get_thinking_budget_tokens,
        get_thinking_level,
    )
    from evals.common import config as cfg

    cfg.get_eval_config.cache_clear()
    for level, expected in (("off", 0), ("low", 1024), ("medium", 5000), ("high", 10000)):
        monkeypatch.setenv(config.DEEPAGENTS_THINKING_LEVEL_ENV, level)
        cfg.get_eval_config.cache_clear()
        assert get_thinking_level() == level
        assert get_thinking_budget_tokens() == expected
    cfg.get_eval_config.cache_clear()


def test_build_chat_model_includes_thinking_when_budget_positive(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """thinking is wired through init_chat_model only when budget > 0."""
    from evals.agents.baseline import build_chat_model

    captured: dict[str, object] = {}

    def fake_init_chat_model(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "stub"

    import evals.agents.baseline as mod

    monkeypatch.setattr(mod, "init_chat_model", fake_init_chat_model)

    # budget > 0 → thinking field included
    build_chat_model("anthropic:test", thinking_budget_tokens=5000)
    assert captured["kwargs"]["thinking"] == {"type": "enabled", "budget_tokens": 5000}

    # budget == 0 → thinking field omitted entirely (non-Anthropic gateway compat)
    captured.clear()
    build_chat_model("anthropic:test", thinking_budget_tokens=0)
    assert "thinking" not in captured["kwargs"]


def test_build_baseline_agent_wraps_with_finalizer_when_response_format_set(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """response_format wires the structured-finalizer wrapper, not deepagents binding.

    The Anthropic API rejects ``tool_choice=forced`` + ``thinking=enabled``
    simultaneously. The old workaround auto-disabled thinking whenever
    ``response_format`` was set — losing reasoning quality on QA/review.
    The new contract: thinking stays on, ``response_format`` is **not**
    passed to deepagents, and a post-loop structured-output finalizer
    enforces the schema instead. This test pins both halves so the fix
    doesn't silently regress to the old conflict.
    """
    from pydantic import BaseModel

    import evals.agents.baseline as mod
    from evals.agents.baseline import build_baseline_agent
    from evals.agents.structured_finalizer import _StructuredFinalizerWrapper

    captured_chat: dict[str, object] = {}
    captured_agent: dict[str, object] = {}

    def fake_build_chat_model(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured_chat["kwargs"] = kwargs
        return "stub-chat-model"

    def fake_create_deep_agent(**kw):  # type: ignore[no-untyped-def]
        captured_agent["kwargs"] = kw
        return "stub-agent"

    monkeypatch.setattr(mod, "build_chat_model", fake_build_chat_model)
    monkeypatch.setattr(mod, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(mod, "_register_baseline_profile", lambda _model: None)

    class _R(BaseModel):
        findings: list[str] = []

    # With response_format set: thinking stays on, deepagents gets no
    # response_format, and the return value is the wrapper.
    agent = build_baseline_agent(
        system_prompt="x",
        model="anthropic:test",
        response_format=_R,
        thinking_budget_tokens=5000,
    )
    assert captured_chat["kwargs"]["thinking_budget_tokens"] == 5000
    assert "response_format" not in captured_agent["kwargs"]
    assert isinstance(agent, _StructuredFinalizerWrapper)

    # Without response_format → no wrapper, thinking stays.
    captured_chat.clear()
    captured_agent.clear()
    agent = build_baseline_agent(
        system_prompt="x",
        model="anthropic:test",
        thinking_budget_tokens=5000,
    )
    assert captured_chat["kwargs"]["thinking_budget_tokens"] == 5000
    assert agent == "stub-agent"


def test_build_run_config_records_unprefixed_model_for_langsmith() -> None:
    """``model`` in RunnableConfig metadata is the user-facing label.

    Internal LangChain routing still uses the prefixed form via
    ``init_chat_model("anthropic:glm-4.5-air", …)`` — but LangSmith trace
    attributes should show the model the gateway actually served
    (``glm-4.5-air``), not our internal client-selection label.
    """
    from evals.agents.baseline import build_run_config

    cfg = build_run_config(
        sample_id="s1",
        dataset_version="dv",
        solver_family="sf",
        model="anthropic:glm-4.5-air",
    )
    assert cfg["metadata"]["model"] == "glm-4.5-air"
