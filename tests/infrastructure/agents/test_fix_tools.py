"""make_fix_tools — per-event tool factory for DeepAgentsFixResponder.

These tests use a hand-rolled StubSandbox that satisfies SandboxPort,
because we want to assert tool↔sandbox wiring without dragging in either
the fake (subprocess + tempdir) or Daytona (MagicMock) machinery.

ToolBudget is retired — budget enforcement is now handled by
ToolCallLimitMiddleware in the runtime stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openbot.application.ports.sandbox import ExecResult
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.agents._fix_tools import make_fix_tools


@dataclass
class _StubSandbox:
    """Minimal SandboxPort impl that records calls and replays canned results."""

    workspace: str = "/workspace/repo"
    files: dict[str, str] = field(default_factory=dict)
    run_calls: list[list[str]] = field(default_factory=list)
    run_result: ExecResult = field(
        default_factory=lambda: ExecResult(stdout="", stderr="", exit_code=0, timed_out=False)
    )
    listing: list[str] = field(default_factory=list)
    diff_text: str = ""

    async def clone(self, *args: object, **kwargs: object) -> None:
        # Signature kept loose on purpose — the test asserts that fix
        # *tools* never reach back into the sandbox to re-clone, so any
        # call here is a contract violation regardless of args.
        raise AssertionError("tools must not call clone()")

    async def read_file(self, path: str) -> str:
        return self.files.get(path, "")

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def list_files(self, *, path: str = ".", max: int = 200) -> list[str]:
        return list(self.listing)

    async def run(
        self,
        *,
        command: list[str],
        timeout_seconds: int = 60,
        env: Any = None,
    ) -> ExecResult:
        self.run_calls.append(command)
        return self.run_result

    async def git_diff(self) -> str:
        return self.diff_text

    async def commit_and_push(self, *, branch_ref: str, message: str, token: str) -> None:
        raise AssertionError("tools must not call commit_and_push()")

    async def close(self) -> None:  # pragma: no cover
        raise AssertionError("tools must not call close()")


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d",
        kind=EventKind.ISSUE_ASSIGNED,
        repo="YiAgent/openbot",
        actor="someone",
        issue_number=42,
        installation_id=1,
    )


def _tool(tools: list[Any], name: str) -> Any:
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"missing tool: {name}")


def test_make_fix_tools_returns_expected_set() -> None:
    sandbox = _StubSandbox()
    tools = make_fix_tools(sandbox=sandbox, event=_event())
    names = sorted(t.name for t in tools)
    assert names == [
        "git_diff",
        "list_files",
        "read_file",
        "run_command",
        "search_files",
        "write_file",
    ]


async def test_read_and_write_file_round_trip() -> None:
    sandbox = _StubSandbox(files={"src/app.py": "old"})
    tools = make_fix_tools(sandbox=sandbox, event=_event())
    read = _tool(tools, "read_file")
    write = _tool(tools, "write_file")

    assert await read.coroutine(path="src/app.py") == "old"
    await write.coroutine(path="src/app.py", content="new\n")
    assert sandbox.files["src/app.py"] == "new\n"


async def test_run_command_executes_via_sandbox() -> None:
    sandbox = _StubSandbox(
        run_result=ExecResult(stdout="3 passed", stderr="", exit_code=0, timed_out=False)
    )
    tools = make_fix_tools(sandbox=sandbox, event=_event())
    run_command = _tool(tools, "run_command")

    result = await run_command.coroutine(command=["pytest", "-q"], timeout_seconds=30)

    assert sandbox.run_calls == [["pytest", "-q"]]
    assert result["exit_code"] == 0
    assert result["stdout"] == "3 passed"
    assert result["timed_out"] is False


async def test_search_files_wraps_grep() -> None:
    sandbox = _StubSandbox(
        run_result=ExecResult(
            stdout="src/a.py:12:foo\nsrc/b.py:33:foo\n",
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    tools = make_fix_tools(sandbox=sandbox, event=_event())
    search = _tool(tools, "search_files")

    hits = await search.coroutine(pattern="foo", path_glob="src/**")

    assert hits == ["src/a.py:12:foo", "src/b.py:33:foo"]
    # First two argv positions are `grep -rn`; arbitrary glob handling
    # is the sandbox's job, not the tool's.
    assert sandbox.run_calls[0][:2] == ["grep", "-rn"]


async def test_make_fix_tools_returns_six_tools() -> None:
    tools = make_fix_tools(sandbox=_StubSandbox(), event=_event())  # type: ignore[arg-type]
    names = {t.name for t in tools}
    assert names == {
        "read_file",
        "write_file",
        "list_files",
        "run_command",
        "git_diff",
        "search_files",
    }


async def test_git_diff_returns_sandbox_diff() -> None:
    sandbox = _StubSandbox(diff_text="diff --git a/x b/x\n+hello\n")
    tools = make_fix_tools(sandbox=sandbox, event=_event())
    diff = _tool(tools, "git_diff")
    assert (await diff.coroutine()).startswith("diff --git")
