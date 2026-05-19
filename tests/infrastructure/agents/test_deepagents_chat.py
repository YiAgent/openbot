from __future__ import annotations

from types import SimpleNamespace
from typing import Any

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


async def test_deepagents_chat_responder_builds_agent_with_chat_model(
    monkeypatch,
) -> None:
    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
            seen["payload"] = payload
            return {"messages": [SimpleNamespace(content="Final answer from DeepAgents")]}

    def _fake_create_deep_agent(**kwargs: Any) -> _Agent:
        seen["kwargs"] = kwargs
        return _Agent()

    import openbot.infrastructure.agents.deepagents_chat as mod

    monkeypatch.setattr(mod, "create_deep_agent", _fake_create_deep_agent)
    mod._agent_for_model.cache_clear()

    responder = mod.DeepAgentsChatResponder()
    reply = await responder.reply_for_event(_event(), user_request="summarize this")

    assert reply == "Final answer from DeepAgents"
    assert seen["kwargs"]["model"] == "anthropic:claude-sonnet-4-6"
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
        async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
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
    mod._agent_for_model.cache_clear()

    responder = mod.DeepAgentsChatResponder()
    reply = await responder.reply_for_event(_event(), user_request="summarize this")

    assert reply == "First. Second."
