from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from openbot.domain.events import EventKind, UnifiedEvent

# Default budget sentinel for tests that don't exercise the budget path.
_CAP = Decimal("5.00")


def _event_simple() -> UnifiedEvent:
    """Minimal event for the new runtime-based tests."""
    return UnifiedEvent(
        channel="github",
        delivery_id="c-1",
        kind=EventKind.ISSUE_COMMENT_CREATED,
        repo="o/r",
        actor="bob",
        installation_id=1,
    )


def _event(*, comment_body: str = "@openbot summarize this") -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="chat-deliv-1",
        kind=EventKind.ISSUE_COMMENT_CREATED,
        repo="YiAgent/openbot",
        actor="yiwang",
        issue_number=42,
        comment_body=comment_body,
        installation_id=101,
    )


def _make_fake_msg(text: str) -> SimpleNamespace:
    """Return a minimal message-like object with a ``content`` string."""
    return SimpleNamespace(content=text)


async def test_deepagents_chat_responder_builds_agent_with_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(self, payload: dict[str, Any], *, config: Any = None) -> dict[str, Any]:
            seen["payload"] = payload
            return {"messages": [_make_fake_msg("Final answer from DeepAgents")]}

    def _fake_create_deep_agent(**kwargs: Any) -> _Agent:
        seen["kwargs"] = kwargs
        return _Agent()

    import openbot.infrastructure.agents.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "create_deep_agent", _fake_create_deep_agent)

    from openbot.infrastructure.agents.deepagents_chat import DeepAgentsChatResponder

    responder = DeepAgentsChatResponder()
    reply = await responder.reply_for_event(
        _event(), user_request="summarize this", per_task_cap_usd=_CAP, session_factory=None
    )

    assert reply == "Final answer from DeepAgents"
    # Runtime uses normalize_for_langchain("anthropic/GLM-5.1") → "anthropic:GLM-5.1"
    # Model is passed as a BaseChatModel instance (built by build_agent_chat_model), not a string
    assert seen["kwargs"]["tools"] == []
    assert "GitHub maintainer bot" in seen["kwargs"]["system_prompt"]
    prompt = seen["payload"]["messages"][0]["content"]
    assert "YiAgent/openbot" in prompt
    assert "issue #42" in prompt
    assert "summarize this" in prompt


async def test_deepagents_chat_responder_extracts_text_from_block_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Agent:
        async def ainvoke(self, payload: dict[str, Any], *, config: Any = None) -> dict[str, Any]:
            return {
                "messages": [
                    SimpleNamespace(
                        content=[
                            {"type": "text", "text": "First."},
                            {"type": "text", "text": " Second."},
                        ]
                    )
                ]
            }

    import openbot.infrastructure.agents.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: _Agent())

    from openbot.infrastructure.agents.deepagents_chat import DeepAgentsChatResponder

    responder = DeepAgentsChatResponder()
    reply = await responder.reply_for_event(
        _event(), user_request="summarize this", per_task_cap_usd=_CAP, session_factory=None
    )

    assert reply == "First. Second."


async def test_chat_responder_passes_checkpointer_and_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    import openbot.infrastructure.agents.runtime as runtime_mod
    from openbot.infrastructure.agents.deepagents_chat import DeepAgentsChatResponder

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, *, config: Any = None) -> dict[str, Any]:
            captured["config"] = config
            return {"messages": [_make_fake_msg("pong")]}

    def fake_create(
        *, model: Any, tools: Any, system_prompt: Any, checkpointer: Any = None, **kw: Any
    ) -> FakeAgent:
        captured["checkpointer"] = checkpointer
        return FakeAgent()

    monkeypatch.setattr(runtime_mod, "create_deep_agent", fake_create)

    saver = MemorySaver()
    # ChatProfile has checkpoint_enabled=False, so checkpointer is NOT forwarded.
    # This is intentional — the chat profile is stateless.
    responder = DeepAgentsChatResponder()
    await responder.reply_for_event(
        _event(),
        user_request="hello",
        run_id="run-chat-1",
        checkpointer=saver,
        per_task_cap_usd=_CAP,
        session_factory=None,
    )

    # ChatProfile.checkpoint_enabled is False → runtime ignores checkpointer
    assert captured["checkpointer"] is None
    # recursion_limit is always set by the runtime
    assert captured["config"]["recursion_limit"] > 0


async def test_chat_responder_no_checkpointer_no_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When run_id/checkpointer are None, ainvoke still receives recursion_limit in config.

    This is the key bug fix: the old implementation passed `config or None` which meant
    LangGraph received no recursion_limit. The runtime now always sets it.
    """
    import openbot.infrastructure.agents.runtime as runtime_mod
    from openbot.infrastructure.agents.deepagents_chat import DeepAgentsChatResponder

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, *, config: Any = None) -> dict[str, Any]:
            captured["config"] = config
            return {"messages": [_make_fake_msg("ok")]}

    def fake_create(
        *, model: Any, tools: Any, system_prompt: Any, checkpointer: Any = None, **kw: Any
    ) -> FakeAgent:
        captured["checkpointer"] = checkpointer
        return FakeAgent()

    monkeypatch.setattr(runtime_mod, "create_deep_agent", fake_create)

    responder = DeepAgentsChatResponder()
    await responder.reply_for_event(
        _event(), user_request="hi", per_task_cap_usd=_CAP, session_factory=None
    )

    assert captured["checkpointer"] is None
    # Bug fix: config must never be None — recursion_limit is always set
    assert captured["config"] is not None
    assert captured["config"]["recursion_limit"] > 0


async def test_chat_responder_rebuilds_agent_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every call must produce a fresh agent — no caching."""
    import openbot.infrastructure.agents.runtime as runtime_mod
    from openbot.infrastructure.agents.deepagents_chat import DeepAgentsChatResponder

    builds: list[Any] = []

    class FakeAgent:
        async def ainvoke(self, payload: Any, *, config: Any = None) -> dict[str, Any]:
            return {"messages": [_make_fake_msg("ok")]}

    def fake_create(
        *, model: Any, tools: Any, system_prompt: Any, checkpointer: Any = None, **kw: Any
    ) -> FakeAgent:
        builds.append(model)
        return FakeAgent()

    monkeypatch.setattr(runtime_mod, "create_deep_agent", fake_create)

    responder = DeepAgentsChatResponder()
    await responder.reply_for_event(
        _event(), user_request="a", per_task_cap_usd=_CAP, session_factory=None
    )
    await responder.reply_for_event(
        _event(), user_request="b", per_task_cap_usd=_CAP, session_factory=None
    )

    assert len(builds) == 2, "Agent must be rebuilt per call — no caching"


async def test_chat_profile_sets_recursion_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chat must receive a recursion_limit — the old None/empty config was a bug."""
    import openbot.infrastructure.agents.runtime as runtime_mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, *, config: Any = None) -> dict[str, Any]:
            captured["config"] = config
            msg = type("M", (), {"content": "hello"})()
            return {"messages": [msg]}

    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: FakeAgent())

    from openbot.infrastructure.agents.deepagents_chat import DeepAgentsChatResponder

    await DeepAgentsChatResponder().reply_for_event(
        _event_simple(), user_request="Hello!", per_task_cap_usd=_CAP, session_factory=None
    )

    assert captured.get("config") is not None, "config must not be None"
    assert "recursion_limit" in captured["config"], "recursion_limit must be set in config"
    assert captured["config"]["recursion_limit"] > 0


async def test_chat_responder_delegates_to_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compatibility wrapper must delegate to BaseDeepAgentRuntime.run."""
    from openbot.infrastructure.agents.deepagents_chat import ChatProfile, DeepAgentsChatResponder
    from openbot.infrastructure.agents.runtime import BaseDeepAgentRuntime

    run_calls: list[Any] = []

    async def fake_run(self: Any, profile: Any, request: Any) -> str:
        run_calls.append((profile, request))
        return "delegated reply"

    monkeypatch.setattr(BaseDeepAgentRuntime, "run", fake_run)

    result = await DeepAgentsChatResponder().reply_for_event(
        _event_simple(), user_request="Hello!", per_task_cap_usd=_CAP, session_factory=None
    )

    assert len(run_calls) == 1
    assert isinstance(run_calls[0][0], ChatProfile)
    assert result == "delegated reply"
