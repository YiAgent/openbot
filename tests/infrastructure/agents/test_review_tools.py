"""Review tool wrappers — port-method bindings + per-run budget guard.

Tools close over ``(adapter, event, budget)`` so the deepagents runtime
can invoke them without knowing about either. The budget guard caps total
tool invocations per review run — opus-4-7 will happily issue 30+ greps
if asked, and each one hits api.github.com.
"""

from __future__ import annotations

from typing import Any

import pytest

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.agents._review_tools import (
    DEFAULT_TOOL_BUDGET,
    ToolBudget,
    ToolBudgetExceededError,
    make_review_tools,
)


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


async def test_make_review_tools_exposes_read_file_and_grep_repo() -> None:
    adapter = _RecordingAdapter()
    tools = make_review_tools(adapter=adapter, event=_event())
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


async def test_budget_blocks_excess_tool_calls() -> None:
    adapter = _RecordingAdapter()
    budget = ToolBudget(remaining=2)
    tools = make_review_tools(adapter=adapter, event=_event(), budget=budget)
    read = _tool_by_name(tools, "read_file")
    grep = _tool_by_name(tools, "grep_repo")

    await read.ainvoke({"path": "a"})
    await grep.ainvoke({"pattern": "x"})

    with pytest.raises(ToolBudgetExceededError):
        await read.ainvoke({"path": "b"})

    # Adapter only saw the two pre-budget calls; the third was blocked.
    assert adapter.read_calls == ["a"]
    assert len(adapter.grep_calls) == 1


async def test_default_budget_matches_documented_cap() -> None:
    # The docstring + plan promise a small cap so opus-4-7 can't grep-spam.
    # Whatever the number is, freeze it here so a future bump is intentional.
    assert DEFAULT_TOOL_BUDGET == 5


async def test_budget_is_per_responder_call_not_global() -> None:
    adapter = _RecordingAdapter()
    tools1 = make_review_tools(adapter=adapter, event=_event())
    tools2 = make_review_tools(adapter=adapter, event=_event())

    # Consume the first tool-set's budget entirely.
    read1 = _tool_by_name(tools1, "read_file")
    for i in range(DEFAULT_TOOL_BUDGET):
        await read1.ainvoke({"path": f"f{i}"})
    with pytest.raises(ToolBudgetExceededError):
        await read1.ainvoke({"path": "x"})

    # The second tool-set should have its own fresh budget.
    await _tool_by_name(tools2, "read_file").ainvoke({"path": "y"})
    assert "y" in adapter.read_calls
