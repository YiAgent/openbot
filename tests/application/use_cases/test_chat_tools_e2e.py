"""End-to-end chat: action refusal + grounded answer with fake adapter.

These tests use a fake DeepAgents runtime so the assertion is on the
profile + tool wiring, not on any specific LLM output. The point is that:

  - the chat workflow plumbs ctx.adapter into the responder,
  - the responder builds three tools,
  - an action request like "open a PR" reaches a refusal, not a sandbox call,
  - a grounded request like "where is config loaded?" hits read_file/grep_repo
    at least once.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from openbot.application.middleware import PreflightContext
from openbot.application.router import Dispatch, derive_task_id
from openbot.application.use_cases.chat import maybe_run_chat
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from openbot.infrastructure.config_loader import baked_in_defaults


class _RecordingAdapter:
    name = "recording"

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.read_calls: list[str] = []
        self.grep_calls: list[tuple[str, str | None]] = []
        self.created_prs: list[dict[str, Any]] = []

    async def reply(self, _event: UnifiedEvent, message: str) -> dict[str, Any]:
        self.replies.append(message)
        return {"id": 1}

    async def update_comment(
        self, _event: UnifiedEvent, comment_id: int | str, message: str
    ) -> dict[str, Any]:
        self.replies.append(message)
        return {"id": comment_id}

    async def read_file(self, _event: UnifiedEvent, path: str) -> str:
        self.read_calls.append(path)
        return "config_loader.py: loads .openbot/config.yaml"

    async def grep_repo(
        self,
        _event: UnifiedEvent,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        self.grep_calls.append((pattern, path_glob))
        return ["openbot/infrastructure/config_loader.py: load_for_repo"]

    async def open_pull_request(self, *_args: Any, **_kw: Any) -> dict[str, Any]:
        self.created_prs.append({"args": _args, "kw": _kw})
        return {"number": 99}


def _event(body: str) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        kind=EventKind.ISSUE_COMMENT_CREATED,
        delivery_id="d1",
        repo="acme/web",
        actor="alice",
        installation_id=1,
        issue_number=7,
        pr_number=None,
        comment_body=body,
    )


def _ctx(adapter: _RecordingAdapter, body: str) -> PreflightContext:
    event = _event(body)
    return PreflightContext(
        event=event,
        dispatch=Dispatch(Feature.CHAT, maybe_run_chat, derive_task_id(event)),
        config=baked_in_defaults(),
        adapter=adapter,  # type: ignore[arg-type]
        session_factory=None,
        redis=None,
    )


async def test_chat_refuses_action_request() -> None:
    adapter = _RecordingAdapter()
    ctx = _ctx(adapter, "@openbot open a PR that fixes this")
    refusal = "[openbot] Chat is read-only — assign this issue to @openbot to trigger fix."
    with patch(
        "openbot.application.use_cases.chat._generate_freeform_reply",
        new=AsyncMock(return_value=refusal),
    ):
        await maybe_run_chat(ctx)
    assert any("read-only" in r.lower() for r in adapter.replies)
    assert adapter.created_prs == [], "action request must not reach open_pull_request"


async def test_chat_grounded_answer_invokes_tools() -> None:
    """Tool invocation is asserted at the *profile* level — full LLM-driven
    invocation lives in evals, not unit tests. We verify here that the chat
    profile's build_tools, given a real adapter, returns three tools that
    successfully call the adapter."""
    from openbot.infrastructure.agents._chat_tools import make_chat_tools

    adapter = _RecordingAdapter()
    event = _event("@openbot where is config loaded?")
    tools = make_chat_tools(adapter=adapter, event=event)  # type: ignore[arg-type]
    read = next(t for t in tools if t.name == "read_file")
    grep = next(t for t in tools if t.name == "grep_repo")
    await read.ainvoke({"path": "openbot/infrastructure/config_loader.py"})
    await grep.ainvoke({"pattern": "load_for_repo"})
    assert adapter.read_calls == ["openbot/infrastructure/config_loader.py"]
    assert adapter.grep_calls == [("load_for_repo", None)]
