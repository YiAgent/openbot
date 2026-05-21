from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openbot.application.middleware import PreflightContext
from openbot.application.router import Dispatch, derive_task_id
from openbot.application.use_cases.chat import maybe_run_chat
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from openbot.infrastructure.config_loader import baked_in_defaults


def _event(*, comment_body: str) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="chat-deliv-1",
        kind=EventKind.ISSUE_COMMENT_CREATED,
        repo="YiAgent/openbot",
        actor="yiwang",
        issue_number=42,
        comment_body=comment_body,
        installation_id=100,
    )


def _adapter() -> Any:
    adapter = AsyncMock()
    adapter.reply = AsyncMock(return_value={"id": 4321})
    return adapter


def _ctx(adapter: Any, event: UnifiedEvent) -> PreflightContext:
    return PreflightContext(
        event=event,
        dispatch=Dispatch(Feature.CHAT, maybe_run_chat, derive_task_id(event)),
        config=baked_in_defaults(),
        adapter=adapter,
        session_factory=None,
        redis=None,
    )


async def test_help_command_keeps_canned_reply() -> None:
    adapter = _adapter()

    await maybe_run_chat(_ctx(adapter, _event(comment_body="@openbot help")))

    _, message = adapter.reply.await_args.args
    assert "OpenBot chat" in message
    assert "Usage: `@openbot <your question or request>`" in message


async def test_freeform_chat_uses_deepagents_reply(monkeypatch) -> None:
    adapter = _adapter()

    async def _fake_reply(*, event: UnifiedEvent, user_request: str) -> str:
        assert user_request == "summarize this thread"
        return "DeepAgents says hello."

    import openbot.application.use_cases.chat as mod

    monkeypatch.setattr(mod, "_generate_freeform_reply", _fake_reply)

    await maybe_run_chat(_ctx(adapter, _event(comment_body="@openbot summarize this thread")))

    _, message = adapter.reply.await_args.args
    assert message == "DeepAgents says hello."


async def test_freeform_chat_falls_back_to_error_reply_when_agent_fails(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter()

    async def _boom(*, event: UnifiedEvent, user_request: str) -> str:
        raise RuntimeError("missing llm credentials")

    import openbot.application.use_cases.chat as mod

    monkeypatch.setattr(mod, "_generate_freeform_reply", _boom)

    with caplog.at_level(logging.ERROR, logger="openbot.application.use_cases.chat"):
        await maybe_run_chat(_ctx(adapter, _event(comment_body="@openbot summarize this thread")))

    _, message = adapter.reply.await_args.args
    assert "couldn't complete that request right now" in message
    assert any(r.message == "chat_agent_reply_failed" for r in caplog.records)
