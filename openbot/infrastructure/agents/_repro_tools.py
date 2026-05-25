# openbot/infrastructure/agents/_repro_tools.py
"""LangChain tool wrappers for the reproduce agent — read-only subset.

The reproduce loop only needs to *inspect* the repo and *execute* the
user's reproduction command; it must never mutate the workspace or
produce a diff. Surface is therefore narrower than ``make_fix_tools``:

    {read_file, list_files, run_command}

The three closures below are deliberate copies of the same closures in
``_fix_tools`` rather than re-exports — the spec asks for "semantics
identical to ``_fix_tools``" and copying keeps that contract stable
without coupling the fix-tool factory to a read-only consumer. If a
behavioural fix lands in ``_fix_tools.read_file`` / ``list_files`` /
``run_command``, mirror it here (and the contract test in
``tests/infrastructure/agents/test_repro_tools.py`` will catch a
forbidden-surface regression).

Budget is enforced by ToolCallLimitMiddleware at the runtime layer —
this module deliberately does no rate limiting of its own.
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


def make_repro_tools(
    *,
    sandbox: SandboxPort,
    event: UnifiedEvent,  # reserved for per-event logging, mirrors _fix_tools
) -> list[StructuredTool]:
    """Build the per-run reproduce tool list (read-only)."""

    async def read_file(path: str) -> str:
        return await sandbox.read_file(path)

    async def list_files(path: str = ".", max: int = 200) -> list[str]:
        return await sandbox.list_files(path=path, max=max)

    async def run_command(command: list[str], timeout_seconds: int = 60) -> dict[str, Any]:
        # Clamp LLM-supplied timeout to prevent a single tool call from
        # occupying a thread-pool slot for an unbounded duration. The
        # agent-level wall_seconds=180 ceiling (AgentRunLimits) guards
        # the full run, but an individual call could stall the asyncio
        # thread pool before that limit fires.
        result = await sandbox.run(command=command, timeout_seconds=min(timeout_seconds, 300))
        return _exec_result_to_dict(result)

    return [
        StructuredTool.from_function(
            coroutine=read_file,
            name="read_file",
            description="Read a UTF-8 file from the sandbox workspace.",
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
    ]


__all__ = ["make_repro_tools"]
