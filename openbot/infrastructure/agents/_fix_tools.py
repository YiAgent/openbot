# openbot/infrastructure/agents/_fix_tools.py
"""LangChain tool wrappers for the fix agent.

Tools close over (sandbox, event). Budget is enforced by
ToolCallLimitMiddleware in the runtime stack — ToolBudget is retired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.tools import StructuredTool

if TYPE_CHECKING:
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.domain.events import UnifiedEvent


def _exec_result_to_dict(result: Any) -> dict[str, Any]:
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
) -> list[StructuredTool]:
    """Build the per-run fix tool list."""

    async def read_file(path: str) -> str:
        return await sandbox.read_file(path)

    async def write_file(path: str, content: str) -> str:
        await sandbox.write_file(path, content)
        return f"wrote {len(content)} bytes to {path}"

    async def list_files(path: str = ".", max: int = 200) -> list[str]:
        return await sandbox.list_files(path=path, max=max)

    async def run_command(command: list[str], timeout_seconds: int = 60) -> dict[str, Any]:
        # Clamp LLM-supplied timeout to prevent a single tool call from
        # occupying a thread-pool slot for an unbounded duration.  The
        # agent-level wall_seconds ceiling (AgentRunLimits.wall_seconds)
        # guards the full run, but an individual call could stall the
        # asyncio thread pool before that limit fires.
        result = await sandbox.run(command=command, timeout_seconds=min(timeout_seconds, 300))
        return _exec_result_to_dict(result)

    async def git_diff() -> str:
        return await sandbox.git_diff()

    async def search_files(pattern: str, path_glob: str | None = None) -> list[str]:
        cmd: list[str] = ["grep", "-rn", pattern]
        if path_glob:
            cmd.extend(["--include", path_glob])
        cmd.append(".")
        result = await sandbox.run(command=cmd, timeout_seconds=30)
        if result.exit_code not in (0, 1):
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    return [
        StructuredTool.from_function(
            coroutine=read_file,
            name="read_file",
            description="Read a UTF-8 file from the sandbox workspace.",
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
                "{stdout, stderr, exit_code, timed_out}."
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
                "filtered by `path_glob`. Returns lines formatted `path:line:fragment`."
            ),
        ),
    ]


__all__ = ["make_fix_tools"]
