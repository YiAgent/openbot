# tests/infrastructure/agents/test_runtime.py
"""Unit tests for BaseDeepAgentRuntime.

All tests monkeypatch create_deep_agent so no network or LLM calls are made.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.tools import BaseTool

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from openbot.infrastructure.agents.profiles import (
    AgentExecutionError,
    AgentRequest,
    AgentRunLimits,
    AgentSandboxForbiddenError,
    AgentSandboxRequiredError,
    AgentTimeoutError,
    SandboxRequirement,
)

# ── Test helpers ──────────────────────────────────────────────────────────────


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="rt-deliv-1",
        kind=EventKind.PR_OPENED,
        repo="org/repo",
        actor="alice",
        pr_number=7,
        installation_id=1,
    )


def _request(**overrides: Any) -> AgentRequest:
    defaults: dict[str, Any] = {
        "event": _event(),
        "input": {"diff": "--- a/x\n+++ b/x\n"},
    }
    defaults.update(overrides)
    return AgentRequest(**defaults)


@dataclass
class _FakeProfile:
    """Minimal profile for runtime testing."""

    feature: Feature = Feature.REVIEW
    agent_name: str = "review"
    response_schema: Any = None
    limits: AgentRunLimits = field(default_factory=lambda: AgentRunLimits(recursion_limit=5))
    sandbox_requirement: SandboxRequirement = SandboxRequirement.OPTIONAL
    checkpoint_enabled: bool = False
    extra_middleware: Sequence[Any] = ()

    def system_prompt(self, request: AgentRequest) -> str:
        return "You are a test agent."

    def user_message(self, request: AgentRequest) -> str:
        return "Review this diff."

    def build_tools(self, request: AgentRequest) -> Sequence[BaseTool]:
        return []

    def parse_result(self, result: Mapping[str, Any]) -> str:
        messages = result.get("messages", [])
        return getattr(messages[-1], "content", "ok") if messages else "ok"


class _FakeAgent:
    """Fake agent that returns a preset result."""

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self._result = result
        self.invocations: list[tuple[Any, Any]] = []

    async def ainvoke(self, payload: Any, *, config: Any = None) -> dict[str, Any]:
        self.invocations.append((payload, config))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _StubSandbox:
    pass


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_runtime_constructs_chat_model_not_bare_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime must pass a BaseChatModel object, not a string, to create_deep_agent."""
    from langchain_core.language_models import BaseChatModel

    import openbot.infrastructure.agents.runtime as mod

    captured: dict[str, Any] = {}

    def fake_create_deep_agent(*, model: Any, **_: Any) -> _FakeAgent:
        captured["model"] = model
        return _FakeAgent({"messages": []})

    monkeypatch.setattr(mod, "create_deep_agent", fake_create_deep_agent)

    runtime = mod.BaseDeepAgentRuntime()
    profile = _FakeProfile()
    await runtime.run(profile, _request())

    assert isinstance(captured["model"], BaseChatModel), (
        "Runtime must pass a BaseChatModel to create_deep_agent, never a bare string"
    )


async def test_runtime_normalizes_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model passed to build_agent_chat_model is in provider:name form."""
    import openbot.infrastructure.agents.runtime as mod

    captured: dict[str, Any] = {}
    original_build = mod.build_agent_chat_model

    def spy_build(model: str, limits: Any) -> Any:
        captured["model"] = model
        return original_build(model, limits)

    monkeypatch.setattr(mod, "build_agent_chat_model", spy_build)
    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _FakeAgent({"messages": []}))

    runtime = mod.BaseDeepAgentRuntime()
    await runtime.run(_FakeProfile(), _request())

    assert ":" in captured["model"], f"Expected provider:name form, got {captured['model']!r}"
    assert "/" not in captured["model"], f"Slash not normalized: {captured['model']!r}"


async def test_runtime_passes_recursion_limit_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import openbot.infrastructure.agents.runtime as mod

    fake_agent = _FakeAgent({"messages": []})

    def fake_create(**_: Any) -> _FakeAgent:
        return fake_agent

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    profile = _FakeProfile(limits=AgentRunLimits(recursion_limit=17))
    await mod.BaseDeepAgentRuntime().run(profile, _request())

    config = fake_agent.invocations[0][1]
    assert config["recursion_limit"] == 17


async def test_runtime_attaches_checkpoint_when_all_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    import openbot.infrastructure.agents.runtime as mod

    captured: dict[str, Any] = {}
    fake_agent = _FakeAgent({"messages": []})

    def fake_create(*, checkpointer: Any = None, **_: Any) -> _FakeAgent:
        captured["checkpointer"] = checkpointer
        return fake_agent

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    saver = MemorySaver()
    profile = _FakeProfile(checkpoint_enabled=True)
    request = _request(run_id="run-1", checkpointer=saver)
    await mod.BaseDeepAgentRuntime().run(profile, request)

    assert captured["checkpointer"] is saver
    config = fake_agent.invocations[0][1]
    assert config["configurable"]["thread_id"] == "run-1"


async def test_runtime_no_checkpoint_when_run_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    import openbot.infrastructure.agents.runtime as mod

    captured: dict[str, Any] = {}

    def fake_create(*, checkpointer: Any = None, **_: Any) -> _FakeAgent:
        captured["checkpointer"] = checkpointer
        return _FakeAgent({"messages": []})

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    profile = _FakeProfile(checkpoint_enabled=True)
    # checkpointer present but no run_id
    request = _request(checkpointer=MemorySaver())
    await mod.BaseDeepAgentRuntime().run(profile, request)

    assert captured["checkpointer"] is None


async def test_runtime_no_checkpoint_when_profile_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    import openbot.infrastructure.agents.runtime as mod

    captured: dict[str, Any] = {}

    def fake_create(*, checkpointer: Any = None, **_: Any) -> _FakeAgent:
        captured["checkpointer"] = checkpointer
        return _FakeAgent({"messages": []})

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    profile = _FakeProfile(checkpoint_enabled=False)
    request = _request(run_id="run-1", checkpointer=MemorySaver())
    await mod.BaseDeepAgentRuntime().run(profile, request)

    assert captured["checkpointer"] is None


async def test_runtime_raises_when_sandbox_required_but_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _FakeAgent({"messages": []}))

    profile = _FakeProfile(sandbox_requirement=SandboxRequirement.REQUIRED)
    request = _request(sandbox=None)

    with pytest.raises(AgentSandboxRequiredError):
        await mod.BaseDeepAgentRuntime().run(profile, request)


async def test_runtime_raises_when_sandbox_forbidden_but_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _FakeAgent({"messages": []}))

    profile = _FakeProfile(sandbox_requirement=SandboxRequirement.FORBIDDEN)
    request = _request(sandbox=_StubSandbox())

    with pytest.raises(AgentSandboxForbiddenError):
        await mod.BaseDeepAgentRuntime().run(profile, request)


async def test_runtime_calls_parse_result_and_returns_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    raw_result = {"messages": [], "structured_response": "parsed_value"}

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _FakeAgent(raw_result))

    parse_calls: list[Any] = []

    class _TrackingProfile(_FakeProfile):
        def parse_result(self, result: Mapping[str, Any]) -> str:
            parse_calls.append(result)
            return "domain_value"

    result = await mod.BaseDeepAgentRuntime().run(_TrackingProfile(), _request())

    assert result == "domain_value"
    assert len(parse_calls) == 1
    assert parse_calls[0] is raw_result


async def test_runtime_wraps_deepagents_exception_in_agent_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    exc = RuntimeError("deepagents internal error")
    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _FakeAgent(exc))

    with pytest.raises(AgentExecutionError) as exc_info:
        await mod.BaseDeepAgentRuntime().run(_FakeProfile(), _request())

    assert exc_info.value.__cause__ is exc


async def test_runtime_wraps_timeout_in_agent_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    async def _slow_invoke(payload: Any, *, config: Any = None) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"messages": []}

    class _SlowAgent:
        async def ainvoke(self, payload: Any, *, config: Any = None) -> dict[str, Any]:
            return await _slow_invoke(payload, config=config)

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _SlowAgent())

    profile = _FakeProfile(limits=AgentRunLimits(recursion_limit=5, wall_seconds=0))

    with pytest.raises(AgentTimeoutError):
        await mod.BaseDeepAgentRuntime().run(profile, _request())


async def test_runtime_observability_metadata_uses_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as mod

    fake_agent = _FakeAgent({"messages": []})

    def fake_create(**_: Any) -> _FakeAgent:
        return fake_agent

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    await mod.BaseDeepAgentRuntime().run(_FakeProfile(), _request())

    config = fake_agent.invocations[0][1]
    meta = config.get("metadata", {})
    model_in_meta = meta.get("model", "")
    assert ":" not in model_in_meta, (
        f"Metadata model should be display_name (no prefix), got {model_in_meta!r}"
    )


async def test_runtime_soft_finalizes_when_structured_response_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the agent loop ends without a structured_response (e.g. middleware
    truncated the graph), the runtime must run one extra ``with_structured_output``
    pass so the profile still receives a schema-conforming object instead of
    raising AgentStructuredOutputError."""
    from pydantic import BaseModel, Field

    import openbot.infrastructure.agents.runtime as mod

    class _Schema(BaseModel):
        ok: bool = Field(default=False)
        note: str = Field(default="")

    @dataclass
    class _StructuredProfile(_FakeProfile):
        response_schema: Any = _Schema

        def parse_result(self, result: Mapping[str, Any]) -> _Schema:  # type: ignore[override]
            structured = result.get("structured_response")
            if structured is None:
                raise AssertionError("runtime did not soft-finalize")
            assert isinstance(structured, _Schema)
            return structured

    # Agent returns no structured_response — simulating ModelCallLimitMiddleware
    # exit_behavior="end" path where a synthetic AIMessage replaces the
    # response_format node.
    fake_agent = _FakeAgent({"messages": [], "structured_response": None})

    def fake_create(**_: Any) -> _FakeAgent:
        return fake_agent

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    # Stub chat model that exposes with_structured_output → ainvoke.
    class _StubStructured:
        async def ainvoke(self, _messages: Any) -> _Schema:
            return _Schema(ok=True, note="finalized")

    class _StubChatModel:
        def with_structured_output(self, schema: Any) -> _StubStructured:
            assert schema is _Schema
            return _StubStructured()

    monkeypatch.setattr(mod, "build_agent_chat_model", lambda *_a, **_kw: _StubChatModel())

    result = await mod.BaseDeepAgentRuntime().run(_StructuredProfile(), _request())

    assert isinstance(result, _Schema)
    assert result.ok is True
    assert result.note == "finalized"
