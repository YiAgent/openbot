from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openbot.domain.events import EventKind, UnifiedEvent


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
    monkeypatch,
) -> None:
    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(self, payload: dict[str, Any], config: Any = None) -> dict[str, Any]:
            seen["payload"] = payload
            return {"messages": [_make_fake_msg("Final answer from DeepAgents")]}

    def _fake_create_deep_agent(**kwargs: Any) -> _Agent:
        seen["kwargs"] = kwargs
        return _Agent()

    import openbot.infrastructure.agents.deepagents_chat as mod

    monkeypatch.setattr(mod, "create_deep_agent", _fake_create_deep_agent)

    responder = mod.DeepAgentsChatResponder()
    reply = await responder.reply_for_event(_event(), user_request="summarize this")

    assert reply == "Final answer from DeepAgents"
    assert seen["kwargs"]["model"] == "anthropic:GLM-5.1"
    assert seen["kwargs"]["tools"] == []
    assert "GitHub maintainer bot" in seen["kwargs"]["system_prompt"]
    prompt = seen["payload"]["messages"][0]["content"]
    assert "YiAgent/openbot" in prompt
    assert "issue #42" in prompt
    assert "summarize this" in prompt


async def test_deepagents_chat_responder_extracts_text_from_block_content(
    monkeypatch,
) -> None:
    class _Agent:
        async def ainvoke(self, payload: dict[str, Any], config: Any = None) -> dict[str, Any]:
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

    import openbot.infrastructure.agents.deepagents_chat as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _Agent())

    responder = mod.DeepAgentsChatResponder()
    reply = await responder.reply_for_event(_event(), user_request="summarize this")

    assert reply == "First. Second."


async def test_chat_responder_passes_checkpointer_and_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    from openbot.infrastructure.agents import deepagents_chat as mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any = None) -> dict[str, Any]:
            captured["config"] = config
            return {"messages": [_make_fake_msg("pong")]}

    def fake_create(
        *, model: Any, tools: Any, system_prompt: Any, checkpointer: Any = None
    ) -> FakeAgent:
        captured["checkpointer"] = checkpointer
        return FakeAgent()

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    saver = MemorySaver()
    responder = mod.DeepAgentsChatResponder()
    await responder.reply_for_event(
        _event(),
        user_request="hello",
        run_id="run-chat-1",
        checkpointer=saver,
    )

    assert captured["checkpointer"] is saver
    assert captured["config"]["configurable"]["thread_id"] == "run-chat-1"


async def test_chat_responder_no_checkpointer_no_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When run_id/checkpointer are None, ainvoke receives config=None."""
    from openbot.infrastructure.agents import deepagents_chat as mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any = None) -> dict[str, Any]:
            captured["config"] = config
            return {"messages": [_make_fake_msg("ok")]}

    def fake_create(
        *, model: Any, tools: Any, system_prompt: Any, checkpointer: Any = None
    ) -> FakeAgent:
        captured["checkpointer"] = checkpointer
        return FakeAgent()

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    responder = mod.DeepAgentsChatResponder()
    await responder.reply_for_event(_event(), user_request="hi")

    assert captured["checkpointer"] is None
    assert captured["config"] is None


async def test_chat_responder_rebuilds_agent_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After removing lru_cache, every call must produce a fresh agent."""
    from openbot.infrastructure.agents import deepagents_chat as mod

    builds: list[str] = []

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any = None) -> dict[str, Any]:
            return {"messages": [_make_fake_msg("ok")]}

    def fake_create(
        *, model: Any, tools: Any, system_prompt: Any, checkpointer: Any = None
    ) -> FakeAgent:
        builds.append(model)
        return FakeAgent()

    monkeypatch.setattr(mod, "create_deep_agent", fake_create)

    responder = mod.DeepAgentsChatResponder()
    await responder.reply_for_event(_event(), user_request="a")
    await responder.reply_for_event(_event(), user_request="b")

    assert len(builds) == 2, "Agent must be rebuilt per call — lru_cache must be gone"
