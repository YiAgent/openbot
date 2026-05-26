"""make_repro_tools — read-only sandbox tool subset for the reproduce agent.

Mirrors ``tests/infrastructure/agents/test_fix_tools.py`` conventions:
hand-rolled ``_StubSandbox`` that records calls and replays canned
results, so wiring is asserted without dragging in the real sandbox.

The security-critical assertion is the forbidden-set contract — see
``test_make_repro_tools_excludes_mutating_tools`` and the matching
profile test. The reproduce agent must not ship write_file, git_diff,
or search_files; widening the surface is a deliberate code change in
both this file and the profile test.
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
    """Minimal SandboxPort impl — only the read-only surface is needed.

    write_file / git_diff / commit_and_push raise AssertionError so a
    reproduce-tool that accidentally calls into them fails loudly. The
    forbidden-set test asserts those tools aren't *exposed*; these
    asserts add belt-and-braces on the *call* path.
    """

    workspace: str = "/workspace/repo"
    files: dict[str, str] = field(default_factory=dict)
    run_calls: list[dict[str, Any]] = field(default_factory=list)
    run_result: ExecResult = field(
        default_factory=lambda: ExecResult(stdout="", stderr="", exit_code=0, timed_out=False)
    )
    listing: list[str] = field(default_factory=list)

    async def clone(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("repro tools must not call clone()")

    async def read_file(self, path: str) -> str:
        return self.files.get(path, "")

    async def write_file(self, path: str, content: str) -> None:
        raise AssertionError("repro tools must not call write_file()")

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
        raise AssertionError("repro tools must not call git_diff()")

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
def test_make_repro_tools_returns_exactly_three_named_tools() -> None:
    sandbox = _StubSandbox()
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    names = sorted(t.name for t in tools)
    assert names == ["list_files", "read_file", "run_command"]


def test_make_repro_tools_excludes_mutating_tools() -> None:
    """Spec §3.5 forbidden-set: real fix-tool names that would leak or mutate.

    Asserting against the actual ``_fix_tools`` names — not invented
    strings like ``apply_patch`` — keeps the contract honest. If
    ``_fix_tools`` ever renames write_file, this test will still
    enforce the *spirit* of the contract because the new name will
    not appear here, but a paired-rename audit in code review is the
    only way to catch a stealth-rename. See the profile-level contract
    test for the matching assertion.
    """
    sandbox = _StubSandbox()
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    names = {t.name for t in tools}
    assert names.isdisjoint({"write_file", "git_diff", "search_files"}), (
        f"reproduce tool surface must stay read-only; got: {sorted(names)}"
    )


# ── Tool ↔ sandbox wiring ────────────────────────────────────────────────────


async def test_read_file_delegates_to_sandbox() -> None:
    sandbox = _StubSandbox(files={"README.md": "# repo\n"})
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    read_file = _tool(tools, "read_file")
    assert await read_file.coroutine(path="README.md") == "# repo\n"


async def test_list_files_delegates_to_sandbox() -> None:
    sandbox = _StubSandbox(listing=["src/", "tests/", "pyproject.toml"])
    tools = make_repro_tools(sandbox=sandbox, event=_event())  # type: ignore[arg-type]
    list_files = _tool(tools, "list_files")
    assert await list_files.coroutine(path=".", max=10) == [
        "src/",
        "tests/",
        "pyproject.toml",
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
