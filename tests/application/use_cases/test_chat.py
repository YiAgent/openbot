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


def _ctx(
    adapter: Any,
    event: UnifiedEvent,
    *,
    run_id: str | None = None,
    agent_checkpointer: Any = None,
) -> PreflightContext:
    return PreflightContext(
        event=event,
        dispatch=Dispatch(Feature.CHAT, maybe_run_chat, derive_task_id(event), run_id=run_id),
        config=baked_in_defaults(),
        adapter=adapter,
        session_factory=None,
        redis=None,
        agent_checkpointer=agent_checkpointer,
    )


async def test_help_command_keeps_canned_reply() -> None:
    adapter = _adapter()

    await maybe_run_chat(_ctx(adapter, _event(comment_body="@openbot help")))

    _, message = adapter.reply.await_args.args
    assert "OpenBot chat" in message
    assert "Usage: `@openbot <your question or request>`" in message


async def test_freeform_chat_uses_deepagents_reply(monkeypatch) -> None:
    adapter = _adapter()

    async def _fake_reply(
        *,
        event: UnifiedEvent,
        user_request: str,
        run_id: str | None = None,
        checkpointer: Any = None,
    ) -> str:
        assert user_request == "summarize this thread"
        return "DeepAgents says hello."

    import openbot.application.use_cases.chat as mod

    monkeypatch.setattr(mod, "_generate_freeform_reply", _fake_reply)

    await maybe_run_chat(_ctx(adapter, _event(comment_body="@openbot summarize this thread")))

    # Freeform flow: reply() posts the thinking placeholder, then update_comment()
    # delivers the final LLM response via sticky update.
    adapter.reply.assert_awaited_once()  # thinking placeholder
    assert adapter.update_comment.await_args.args[2] == "DeepAgents says hello."


async def test_freeform_chat_falls_back_to_error_reply_when_agent_fails(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _adapter()

    async def _boom(
        *,
        event: UnifiedEvent,
        user_request: str,
        run_id: str | None = None,
        checkpointer: Any = None,
    ) -> str:
        raise RuntimeError("missing llm credentials")

    import openbot.application.use_cases.chat as mod

    monkeypatch.setattr(mod, "_generate_freeform_reply", _boom)

    with caplog.at_level(logging.ERROR, logger="openbot.application.use_cases.chat"):
        await maybe_run_chat(_ctx(adapter, _event(comment_body="@openbot summarize this thread")))

    # Freeform error path: reply() posts thinking placeholder, update_comment()
    # delivers the error template via sticky update.
    adapter.reply.assert_awaited_once()  # thinking placeholder
    assert "couldn't complete that request right now" in adapter.update_comment.await_args.args[2]
    assert any(r.message == "chat_agent_reply_failed" for r in caplog.records)


# ───── checkpointer + run_id threading ─────


async def test_chat_passes_checkpointer_and_run_id_to_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_id and checkpointer from ctx are forwarded to _generate_freeform_reply."""
    captured: dict[str, object] = {}

    async def _fake(
        *,
        event: UnifiedEvent,
        user_request: str,
        run_id: str | None = None,
        checkpointer: Any = None,
    ) -> str:
        captured["run_id"] = run_id
        captured["checkpointer"] = checkpointer
        return "ok"

    import openbot.application.use_cases.chat as mod

    monkeypatch.setattr(mod, "_generate_freeform_reply", _fake)

    sentinel = object()
    adapter = _adapter()
    await maybe_run_chat(
        _ctx(
            adapter,
            _event(comment_body="@openbot explain this"),
            run_id="run-chat-1",
            agent_checkpointer=sentinel,
        )
    )

    assert captured["run_id"] == "run-chat-1"
    assert captured["checkpointer"] is sentinel


async def test_chat_cancellation_checkpoint_fires_after_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """checkpoint() fires after the LLM but before ctx.adapter.reply."""
    import openbot.application.use_cases.chat as mod
    from openbot.application.state.cancellation import RunCancelledError

    calls: list[str] = []

    async def _fake_freeform(
        *,
        event: UnifiedEvent,
        user_request: str,
        run_id: str | None = None,
        checkpointer: Any = None,
    ) -> str:
        calls.append("llm")
        return "answer"

    async def _fake_checkpoint(redis: Any, run_id: str) -> None:
        calls.append("checkpoint")
        raise RunCancelledError("cancelled")

    monkeypatch.setattr(mod, "_generate_freeform_reply", _fake_freeform)
    monkeypatch.setattr(mod, "checkpoint", _fake_checkpoint)

    adapter = _adapter()
    with pytest.raises(RunCancelledError):
        await maybe_run_chat(
            _ctx(adapter, _event(comment_body="@openbot explain this"), run_id="run-chat-2")
        )

    # Freeform sticky flow: reply() posts the thinking placeholder BEFORE the LLM
    # call. Checkpoint fires after LLM, raising RunCancelledError before
    # update_comment() is reached — so the final answer is never delivered.
    assert calls == ["llm", "checkpoint"]
    adapter.reply.assert_awaited_once()  # thinking placeholder was posted
    adapter.update_comment.assert_not_awaited()  # final answer not posted
