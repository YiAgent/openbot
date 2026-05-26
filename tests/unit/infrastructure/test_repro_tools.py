"""make_repro_tools — test-authoring sandbox tool set for the reproduce agent.

Mirrors ``tests/infrastructure/agents/test_fix_tools.py`` conventions:
hand-rolled ``_StubSandbox`` that records calls and replays canned
results, so wiring is asserted without dragging in the real sandbox.

The tool-surface contract is:
  Allowed: read_file, write_file, list_files, run_command, git_diff, search_files
  Forbidden: commit_and_push (repo mutation / push; fix-agent only)

Widening the surface beyond these six tools — or adding commit_and_push —
is a deliberate code change that must be mirrored here and in the
profile test (``test_deepagents_repro.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from openbot.application.ports.sandbox import ExecResult
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.agents._repro_tools import make_repro_tools


@dataclass
class _StubSandbox:
    """Minimal SandboxPort impl — covers the full test-authoring surface.

    commit_and_push raises AssertionError so a reproduce-tool that
    accidentally calls it fails loudly. The tool-set contract test
    asserts that tool is not *exposed*; this adds belt-and-braces on
    the *call* path.
    """

    workspace: str = "/workspace/repo"
    files: dict[str, str] = field(default_factory=dict)
    written: dict[str, str] = field(default_factory=dict)
    run_calls: list[dict[str, Any]] = field(default_factory=list)
    run_result: ExecResult = field(
        default_factory=lambda: ExecResult(stdout="", stderr="", exit_code=0, timed_out=False)
    )
    listing: list[str] = field(default_factory=list)
    diff_result: str = ""

    async def clone(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("repro tools must not call clone()")

    async def read_file(self, path: str) -> str:
        return self.files.get(path, "")

    async def write_file(self, path: str, content: str) -> None:
        self.written[path] = content

    async def list_files(self, *, path: str = ".", max: int = 200) -> list[str]:
        return list(self.listing)

    async def run(
        self,
        *,
        command: list[str],
        timeout_seconds: int = 60,
        env: Any = None,
    ) -> ExecResult:
        self.run_calls.append({"command": command, "timeout_seconds": timeout_seconds})
        return self.run_result

    async def git_diff(self) -> str:
        return self.diff_result

    async def commit_and_push(self, *, branch_ref: str, message: str, token: str) -> None:
        raise AssertionError("repro tools must not call commit_and_push()")

    async def close(self) -> None:  # pragma: no cover
        raise AssertionError("repro tools must not call close()")


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d-repro-1",
        kind=EventKind.ISSUE_OPENED,
        repo="YiAgent/openbot",
        actor="alice",
        issue_number=42,
        installation_id=1,
    )


def _tool(tools: list[Any], name: str) -> Any:
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"missing tool: {name}")


# ── Tool-set contract ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_make_repro_tools_returns_exactly_six_named_tools() -> None:
    sandbox = _StubSandbox()
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    names = sorted(t.name for t in tools)
    assert names == [
        "git_diff",
        "list_files",
        "read_file",
        "run_command",
        "search_files",
        "write_file",
    ]


def test_make_repro_tools_excludes_repo_push_tools() -> None:
    """commit_and_push must never be exposed to the reproduce agent.

    The reproduce agent writes test files and runs them locally.
    It must not be able to push commits to remote — that is the fix
    agent's job. Asserting against the actual sandbox method name keeps
    this contract honest against renames.
    """
    sandbox = _StubSandbox()
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    names = {t.name for t in tools}
    assert "commit_and_push" not in names, (
        f"commit_and_push must not be exposed to the reproduce agent; got: {sorted(names)}"
    )


# ── Tool ↔ sandbox wiring ────────────────────────────────────────────────────


async def test_read_file_delegates_to_sandbox() -> None:
    sandbox = _StubSandbox(files={"README.md": "# repo\n"})
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    read_file = _tool(tools, "read_file")
    assert await read_file.coroutine(path="README.md") == "# repo\n"


async def test_write_file_delegates_to_sandbox() -> None:
    sandbox = _StubSandbox()
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    write_file = _tool(tools, "write_file")
    await write_file.coroutine(path="tests/test_repro_42.py", content="def test_bug(): ...")
    assert sandbox.written == {"tests/test_repro_42.py": "def test_bug(): ..."}


async def test_git_diff_delegates_to_sandbox() -> None:
    diff = "diff --git a/tests/test_repro_42.py b/tests/test_repro_42.py\n+def test_bug(): ..."
    sandbox = _StubSandbox(diff_result=diff)
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    git_diff = _tool(tools, "git_diff")
    assert await git_diff.coroutine() == diff


async def test_list_files_delegates_to_sandbox() -> None:
    sandbox = _StubSandbox(listing=["src/", "tests/", "pyproject.toml"])
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    list_files = _tool(tools, "list_files")
    assert await list_files.coroutine(path=".", max=10) == [
        "src/",
        "tests/",
        "pyproject.toml",
    ]


async def test_search_files_runs_grep_via_sandbox() -> None:
    """search_files shells out to grep via sandbox.run(), not sandbox.search_files().

    The tool parses stdout lines from grep output, so the stub must
    supply the expected grep-style output through run_result.stdout.
    """
    grep_output = "src/api/list.py:42: pop()\nsrc/api/list.py:55:     pop(extra)"
    sandbox = _StubSandbox(
        run_result=ExecResult(stdout=grep_output, stderr="", exit_code=0, timed_out=False)
    )
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    search_files = _tool(tools, "search_files")
    result = await search_files.coroutine(pattern="pop()", path_glob="src/**/*.py")
    assert result == ["src/api/list.py:42: pop()", "src/api/list.py:55:     pop(extra)"]
    # Verify grep was invoked with the pattern and include filter.
    assert sandbox.run_calls[0]["command"] == [
        "grep",
        "-rn",
        "pop()",
        "--include",
        "src/**/*.py",
        ".",
    ]


async def test_run_command_returns_exec_result_dict() -> None:
    sandbox = _StubSandbox(
        run_result=ExecResult(
            stdout="IndexError: list index out of range",
            stderr="",
            exit_code=1,
            timed_out=False,
        )
    )
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    run_command = _tool(tools, "run_command")

    result = await run_command.coroutine(command=["pytest", "-q"], timeout_seconds=30)

    assert sandbox.run_calls == [{"command": ["pytest", "-q"], "timeout_seconds": 30}]
    assert result == {
        "stdout": "IndexError: list index out of range",
        "stderr": "",
        "exit_code": 1,
        "timed_out": False,
    }


async def test_run_command_clamps_timeout_to_300() -> None:
    """LLM-supplied timeout is clamped — mirrors ``_fix_tools.run_command``.

    The agent-level ``wall_seconds=180`` ceiling would catch a runaway
    eventually, but a single tool call could still occupy a thread-pool
    slot well past the agent's budget without this clamp.
    """
    sandbox = _StubSandbox()
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    run_command = _tool(tools, "run_command")

    await run_command.coroutine(command=["sleep", "9999"], timeout_seconds=9999)

    assert sandbox.run_calls == [{"command": ["sleep", "9999"], "timeout_seconds": 300}]
