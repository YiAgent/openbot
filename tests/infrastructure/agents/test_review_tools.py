"""Tests for review agent tool wrappers.

ToolBudget and ToolBudgetExceededError have been retired — budget enforcement
is now handled by ToolCallLimitMiddleware in the runtime stack.
"""

from __future__ import annotations

from typing import Any

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.agents._review_tools import make_review_tools


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="rt-1",
        kind=EventKind.PR_OPENED,
        repo="acme/widgets",
        actor="alice",
        pr_number=42,
        installation_id=99,
    )


class _RecordingAdapter:
    """Stand-in for ChannelAdapterPort that records each tool's call args."""

    def __init__(self) -> None:
        self.read_calls: list[str] = []
        self.grep_calls: list[tuple[str, str | None, int]] = []
        self.file_contents: dict[str, str] = {}
        self.grep_hits: list[str] = []

    async def read_file(self, event: UnifiedEvent, path: str) -> str:
        self.read_calls.append(path)
        return self.file_contents.get(path, "")

    async def grep_repo(
        self,
        event: UnifiedEvent,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        self.grep_calls.append((pattern, path_glob, max_matches))
        return list(self.grep_hits)


def _tool_by_name(tools: list[Any], name: str) -> Any:
    for t in tools:
        if getattr(t, "name", None) == name:
            return t
    raise AssertionError(f"no tool named {name!r} among {[t.name for t in tools]}")


async def test_make_review_tools_returns_two_tools() -> None:
    class _StubAdapter:
        async def read_file(self, event: Any, path: str) -> str:
            return ""

        async def grep_repo(self, event: Any, **kwargs: Any) -> list[str]:
            return []

    event = UnifiedEvent(
        channel="github",
        delivery_id="d",
        kind=EventKind.PR_OPENED,
        repo="o/r",
        actor="alice",
        installation_id=1,
    )
    tools = make_review_tools(adapter=_StubAdapter(), event=event)  # type: ignore[arg-type]
    names = {t.name for t in tools}
    assert names == {"read_file", "grep_repo"}


async def test_read_file_tool_forwards_to_adapter() -> None:
    adapter = _RecordingAdapter()
    adapter.file_contents["src/auth.py"] = "def login(): ..."
    tools = make_review_tools(adapter=adapter, event=_event())

    result = await _tool_by_name(tools, "read_file").ainvoke({"path": "src/auth.py"})

    assert result == "def login(): ..."
    assert adapter.read_calls == ["src/auth.py"]


async def test_grep_repo_tool_forwards_to_adapter() -> None:
    adapter = _RecordingAdapter()
    adapter.grep_hits = ["src/auth.py: def login(...)"]
    tools = make_review_tools(adapter=adapter, event=_event())

    result = await _tool_by_name(tools, "grep_repo").ainvoke(
        {"pattern": "login", "path_glob": "src"}
    )

    assert result == ["src/auth.py: def login(...)"]
    assert adapter.grep_calls == [("login", "src", 20)]


async def test_grep_repo_tool_default_path_glob_is_none() -> None:
    adapter = _RecordingAdapter()
    tools = make_review_tools(adapter=adapter, event=_event())

    await _tool_by_name(tools, "grep_repo").ainvoke({"pattern": "TODO"})

    assert adapter.grep_calls == [("TODO", None, 20)]
