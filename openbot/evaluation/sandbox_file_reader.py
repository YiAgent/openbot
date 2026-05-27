"""SandboxFileReader — serves file reads from a cloned SandboxPort workspace.

Drop-in replacement for ``GitHubFileReader`` in ``EvalChannelAdapter.file_reader``.
Where ``GitHubFileReader`` calls the GitHub Contents API and Code Search API,
``SandboxFileReader`` reads from a locally-cloned workspace — no network
dependency, no rate limits, deterministic at the exact cloned commit.

Usage (typically called by the eval runner, not directly by solvers)::

    async with sandbox_factory() as sandbox:
        await sandbox.clone(repo_url=..., ref=base_sha, token="", strategy=SHALLOW)
        adapter = EvalChannelAdapter(
            pr_diff="",
            file_reader=SandboxFileReader(sandbox),
        )
        result = await agent_responder.do_for_event(event, adapter=adapter, ...)

Compatibility:
  - ``read_file(path)``      — same signature as ``GitHubFileReader.read_file``
  - ``grep_repo(...)``       — same kwargs + return shape as ``GitHubFileReader.grep_repo``
  - ``list_files(path=".")`` — NOT on ``GitHubFileReader`` (which has no directory listing);
    only used when ``EvalChannelAdapter.list_repo_paths`` is called (chat eval).

Output formats match ``GitHubFileReader`` so scorers, tests, and adapter
plumbing need no changes when switching from GitHub-API to sandbox backing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbot.application.ports.sandbox import SandboxPort

logger = logging.getLogger(__name__)


class SandboxFileReader:
    """Duck-typed file reader backed by a live ``SandboxPort`` workspace.

    The sandbox MUST already be cloned before this reader is used — it is
    constructed after ``sandbox.clone(...)`` returns, and stays valid until
    ``sandbox.close()`` is called (handled by the outer ``async with``).
    """

    def __init__(self, sandbox: SandboxPort) -> None:
        self._sandbox = sandbox

    async def read_file(self, path: str) -> str:
        """Return UTF-8 text of ``path`` (workspace-relative), or ``""``."""
        return await self._sandbox.read_file(path)

    async def grep_repo(
        self,
        *,
        pattern: str,
        path_glob: str | None = None,
        max_matches: int = 20,
    ) -> list[str]:
        """Search the workspace for ``pattern`` using ``grep -rn``.

        Returns ``"path: fragment"`` lines (same shape as
        ``GitHubFileReader.grep_repo``) so scorers and adapters need no
        changes.

        Skips ``.git/`` internals and truncates at ``max_matches``.  Returns
        ``[]`` on any execution error (grep exit 1 = "no matches" is normal).
        """
        cmd = [
            "grep",
            "-rn",
            "--color=never",
            "--exclude-dir=.git",
        ]
        if path_glob:
            cmd.extend(["--include", path_glob])
        cmd.extend(["--", pattern, "."])

        try:
            result = await self._sandbox.run(command=cmd, timeout_seconds=30)
        except Exception as exc:
            logger.warning("SandboxFileReader.grep_repo failed: %s", exc)
            return []

        out: list[str] = []
        for line in result.stdout.splitlines():
            # grep -n output: "path:linenum:content"
            parts = line.split(":", 2)
            if len(parts) >= 3:
                path, _linenum, content = parts
                out.append(f"{path}: {content.strip()}")
            elif len(parts) == 2:
                out.append(f"{parts[0]}: {parts[1].strip()}")
            else:
                out.append(line)
            if len(out) >= max_matches:
                break
        return out

    async def list_files(self, path: str = ".") -> list[str]:
        """Return file paths under ``path`` in the workspace (max 200).

        Used by ``EvalChannelAdapter.list_repo_paths`` → chat tool
        ``list_files``.  Not present on ``GitHubFileReader`` (which lacks
        directory listing); the adapter checks via ``hasattr`` before calling.
        """
        try:
            return await self._sandbox.list_files(path=path, max=200)
        except Exception as exc:
            logger.warning("SandboxFileReader.list_files failed: %s", exc)
            return []


__all__ = ["SandboxFileReader"]
