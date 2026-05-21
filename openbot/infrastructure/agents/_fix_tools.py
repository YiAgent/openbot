"""LangChain tool wrappers for DeepAgentsFixResponder (slice C.6).

Six tools, all closing over a per-event ``SandboxPort`` + ``ToolBudget``.
The fix loop's tools are read/write inside the cloned sandbox; none of
them reach GitHub. PR creation and branch push are the use case's job,
not the agent's, so the agent stays confined to the workspace.

Mirrors slice A2's ``_review_tools.py`` shape. The only differences:
  - Higher default budget (20 vs 5) — fix runs need more headroom.
  - Includes write_file / run_command — review tools are read-only.
  - search_files uses sandbox-local grep, not GitHub Code Search,
    because we work inside a cloned tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.tools import StructuredTool

if TYPE_CHECKING:
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.domain.events import UnifiedEvent


# Tighter than it looks: read_file x 6 + write_file x 3 + run_command x 5
# + list_files x 2 + git_diff x 1 + search_files x 3 = 20 typical calls
# for a non-trivial fix. The agent gets one budget item beyond "typical"
# so a single retry is OK; a runaway loop trips immediately.
DEFAULT_FIX_TOOL_BUDGET = 20


class ToolBudgetExceededError(RuntimeError):
    """Raised when a tool call would exceed the per-run budget.

    DeepAgents surfaces this back to the model as a tool error message
    rather than crashing the agent — the model is expected to wrap up
    with whatever findings it has, not retry.
    """

    def __init__(self, tool: str) -> None:
        super().__init__(f"tool budget exhausted on call to {tool!r}")
        self.tool = tool


@dataclass
class ToolBudget:
    """Counts down across tool invocations within a single fix run."""

    remaining: int

    def consume(self, tool: str) -> None:
        if self.remaining <= 0:
            raise ToolBudgetExceededError(tool)
        self.remaining -= 1


def _exec_result_to_dict(result: Any) -> dict[str, Any]:
    """Surface ExecResult as a JSON-friendly dict — agents handle dicts better."""
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
    }


def make_fix_tools(
    *,
    sandbox: SandboxPort,
    event: UnifiedEvent,  # reserved for per-event logging
    budget: ToolBudget | None = None,
) -> list[StructuredTool]:
    """Build the per-run fix tool list.

    All tools close over ``(sandbox, budget)``. ``event`` is accepted but
    unused today — kept in the signature so future logging can include
    per-event identifiers without breaking callers.
    """
    bud = budget if budget is not None else ToolBudget(remaining=DEFAULT_FIX_TOOL_BUDGET)

    async def read_file(path: str) -> str:
        bud.consume("read_file")
        return await sandbox.read_file(path)

    async def write_file(path: str, content: str) -> str:
        bud.consume("write_file")
        await sandbox.write_file(path, content)
        return f"wrote {len(content)} bytes to {path}"

    async def list_files(path: str = ".", max: int = 200) -> list[str]:
        bud.consume("list_files")
        return await sandbox.list_files(path=path, max=max)

    async def run_command(command: list[str], timeout_seconds: int = 60) -> dict[str, Any]:
        bud.consume("run_command")
        result = await sandbox.run(command=command, timeout_seconds=timeout_seconds)
        return _exec_result_to_dict(result)

    async def git_diff() -> str:
        bud.consume("git_diff")
        return await sandbox.git_diff()

    async def search_files(pattern: str, path_glob: str | None = None) -> list[str]:
        bud.consume("search_files")
        cmd: list[str] = ["grep", "-rn", pattern]
        if path_glob:
            cmd.extend(["--include", path_glob])
        cmd.append(".")
        result = await sandbox.run(command=cmd, timeout_seconds=30)
        if result.exit_code not in (0, 1):  # grep exit 1 = no matches
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    return [
        StructuredTool.from_function(
            coroutine=read_file,
            name="read_file",
            description=(
                "Read a UTF-8 file from the sandbox workspace. Returns empty string if missing."
            ),
        ),
        StructuredTool.from_function(
            coroutine=write_file,
            name="write_file",
            description="Replace a file's contents in the sandbox workspace.",
        ),
        StructuredTool.from_function(
            coroutine=list_files,
            name="list_files",
            description="List files under `path` (default '.') up to `max` results.",
        ),
        StructuredTool.from_function(
            coroutine=run_command,
            name="run_command",
            description=(
                "Run an argv-list command in the workspace. Returns "
                "{stdout, stderr, exit_code, timed_out}. Use this to "
                "discover and execute the project's tests."
            ),
        ),
        StructuredTool.from_function(
            coroutine=git_diff,
            name="git_diff",
            description="Return the working-tree diff after your edits.",
        ),
        StructuredTool.from_function(
            coroutine=search_files,
            name="search_files",
            description=(
                "grep -rn for `pattern` across the workspace, optionally "
                "filtered by `path_glob` (a grep --include pattern). Returns "
                "lines formatted `path:line:fragment`."
            ),
        ),
    ]


__all__ = [
    "DEFAULT_FIX_TOOL_BUDGET",
    "ToolBudget",
    "ToolBudgetExceededError",
    "make_fix_tools",
]
