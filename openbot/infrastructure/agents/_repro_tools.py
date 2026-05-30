# openbot/infrastructure/agents/_repro_tools.py
"""LangChain tool wrappers for the reproduce agent.

The reproduce agent writes a test file that captures the bug, runs it to
confirm the failure, then calls ``git_diff`` to produce the patch.  The
tool surface therefore mirrors ``make_fix_tools`` minus any production-code
mutation:

    {read_file, write_file, list_files, run_command, git_diff, search_files}

``write_file`` and ``git_diff`` are intentionally included — the agent's job
is to author a *test* that reproduces the bug, not a production-code fix.
Closures are deliberate copies from ``_fix_tools`` (same rationale: keeps
each surface's contract stable without coupling the implementations).
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
    backend: Any | None = None,
) -> list[StructuredTool]:
    """Build the per-run reproduce tool list.

    When ``backend`` is provided (DeepAgents SandboxBackendProtocol),
    the backend already provides ``read_file``, ``write_file``,
    ``search_files`` (grep), ``ls``, ``glob``, and ``execute``. We keep
    only the tools the backend does NOT cover: ``list_files``, ``run_command``,
    and ``git_diff``.
    """

    async def read_file(path: str) -> str:
        return await sandbox.read_file(path)

    async def write_file(path: str, content: str) -> str:
        await sandbox.write_file(path, content)
        return f"wrote {len(content)} bytes to {path}"

    async def list_files(path: str = ".", max: int = 200) -> list[str]:
        return await sandbox.list_files(path=path, max=max)

    async def run_command(command: list[str], timeout_seconds: int = 60) -> dict[str, Any]:
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

    all_tools = [
        StructuredTool.from_function(
            coroutine=read_file,
            name="read_file",
            description="Read a UTF-8 file from the sandbox workspace.",
        ),
        StructuredTool.from_function(
            coroutine=write_file,
            name="write_file",
            description="Write (or replace) a file in the sandbox workspace.",
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
                "{stdout, stderr, exit_code, timed_out}. "
                "No shell features: no pipes (|), redirects (>, 2>/dev/null), "
                "or operators (&& ||) — each list element is one argument. "
                "exit_code=1 means the command failed normally; "
                "exit_code=4 from pytest means collection error (missing import), not a test failure."
            ),
        ),
        StructuredTool.from_function(
            coroutine=git_diff,
            name="git_diff",
            description="Return the working-tree unified diff after writing your test file.",
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

    if backend is not None:
        # Backend provides read_file, write_file, search_files (grep), ls,
        # glob, execute. Keep only tools the backend does NOT cover.
        backend_covered = {"read_file", "write_file", "search_files"}
        return [t for t in all_tools if t.name not in backend_covered]

    return all_tools


__all__ = ["make_repro_tools"]
