"""Chat profile registers the three v0.1 read-only tools and nothing else."""

from __future__ import annotations

from typing import Any

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.agents.deepagents_chat import ChatProfile
from openbot.infrastructure.agents.profiles import AgentRequest


class _FakeAdapter:
    name = "fake"

    async def read_file(self, _event: UnifiedEvent, path: str) -> str:
        return ""

    async def grep_repo(self, *_args: Any, **_kwargs: Any) -> list[str]:
        return []


def _request(adapter: Any) -> AgentRequest:
    event = UnifiedEvent(
        channel="github",
        kind=EventKind.ISSUE_COMMENT_CREATED,
        delivery_id="d1",
        repo="acme/web",
        actor="alice",
        installation_id=1,
        issue_number=7,
        pr_number=None,
        comment_body="@openbot where is config loaded?",
    )
    return AgentRequest(event=event, input={"user_request": "where?"}, event_adapter=adapter)


def test_build_tools_lists_read_only() -> None:
    tools = ChatProfile().build_tools(_request(_FakeAdapter()))
    names = sorted(t.name for t in tools)
    assert names == ["grep_repo", "list_files", "read_file"]
