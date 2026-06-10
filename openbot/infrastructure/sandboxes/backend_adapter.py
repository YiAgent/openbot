# openbot/infrastructure/sandboxes/backend_adapter.py
"""SandboxBackendAdapter — wraps SandboxPort as a DeepAgents SandboxBackendProtocol.

When ``BaseDeepAgentRuntime`` passes this adapter as ``backend=`` to
``create_deep_agent()``, DeepAgents' built-in tools (``read_file``,
``grep``, ``ls``, ``glob``, ``write``, ``edit``, ``execute``) route
through the sandbox instead of the default empty ``StateBackend``.

Path mapping: DeepAgents uses absolute paths (``/workspace/foo.py``);
``SandboxPort`` expects workspace-relative paths (``foo.py``). The
adapter strips the ``self._workspace`` prefix (or leading ``/``) from
every incoming path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)

if TYPE_CHECKING:
    from openbot.application.ports.sandbox import SandboxPort


class SandboxBackendAdapter(SandboxBackendProtocol):
    """Adapt ``SandboxPort`` to DeepAgents' ``SandboxBackendProtocol``.

    All file operations delegate to the underlying sandbox. Async methods
    are overridden directly (bypassing ``asyncio.to_thread``) because
    ``SandboxPort`` is natively async.
    """

    def __init__(self, sandbox: SandboxPort) -> None:
        self._sandbox = sandbox
        self._workspace = sandbox.workspace.rstrip("/")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _to_relative(self, path: str) -> str:
        """Convert an absolute DeepAgents path to a workspace-relative path.

        Handles:
          - ``/workspace/foo`` → ``foo``  (strip workspace prefix)
          - ``/workspace``     → ````     (exact workspace = root)
          - ``/foo``           → ``foo``  (strip leading ``/``)
          - ``foo``            → ``foo``  (already relative)
        """
        if path == self._workspace:
            return ""
        if path.startswith(self._workspace + "/"):
            return path[len(self._workspace) + 1 :]
        if path.startswith("/"):
            return path.lstrip("/")
        return path

    # ── id property ──────────────────────────────────────────────────────────

    @property
    def id(self) -> str:
        return f"sandbox-adapter-{self._workspace}"

    # ── ls ───────────────────────────────────────────────────────────────────

    async def als(self, path: str) -> LsResult:
        """List immediate children of *path* with ``is_dir`` flags.

        Uses ``os.scandir`` via ``sandbox.run()`` so we get directory
        flags without the recursive behaviour of ``list_files()``.
        """
        rel = self._to_relative(path)
        # Use python -c to get structured listing with is_dir info.
        script = (
            "import os, json, sys\n"
            "path = sys.argv[1] if len(sys.argv) > 1 else '.'\n"
            "try:\n"
            "    entries = []\n"
            "    with os.scandir(path) as it:\n"
            "        for e in it:\n"
            "            try:\n"
            "                entries.append({'path': e.name, 'is_dir': e.is_dir()})\n"
            "            except OSError:\n"
            "                entries.append({'path': e.name, 'is_dir': False})\n"
            "    print(json.dumps(entries))\n"
            "except FileNotFoundError:\n"
            "    print(json.dumps({'error': 'Directory not found: ' + path}))\n"
        )
        result = await self._sandbox.run(
            command=["python3", "-c", script, rel or "."],
            timeout_seconds=10,
        )
        if result.exit_code != 0:
            return LsResult(error=f"ls failed: {result.stderr.strip() or result.stdout.strip()}")

        import json

        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError:
            return LsResult(error=f"ls: invalid JSON output: {result.stdout[:200]}")

        if isinstance(raw, dict) and "error" in raw:
            return LsResult(error=raw["error"])

        entries: list[FileInfo] = [
            FileInfo(path=item["path"], is_dir=item.get("is_dir", False)) for item in raw
        ]
        return LsResult(entries=entries)

    # ── read ─────────────────────────────────────────────────────────────────

    async def aread(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Read file content formatted with ``cat -n`` style line numbers."""
        rel = self._to_relative(file_path)
        raw = await self._sandbox.read_file(rel)
        if raw == "":
            # read_file returns "" for missing or binary files.
            # Try to distinguish: check existence via a quick run.
            check = await self._sandbox.run(
                command=["test", "-f", rel],
                timeout_seconds=5,
            )
            if check.exit_code != 0:
                return ReadResult(error=f"File not found: {file_path}")
            # File exists but is empty or binary — return empty content.
            return ReadResult(file_data={"content": "", "encoding": "utf-8"})

        lines = raw.split("\n")
        # Apply offset/limit
        sliced = lines[offset : offset + limit]
        # Format with line numbers (cat -n style, 1-indexed)
        numbered = "\n".join(f"{i + offset + 1:6d}\t{line}" for i, line in enumerate(sliced))
        # Truncate lines longer than 2000 chars
        truncated_lines = []
        for line in numbered.split("\n"):
            if len(line) > 2000:
                truncated_lines.append(line[:2000] + "… (truncated)")
            else:
                truncated_lines.append(line)
        content = "\n".join(truncated_lines)
        return ReadResult(file_data={"content": content, "encoding": "utf-8"})

    # ── write ────────────────────────────────────────────────────────────────

    async def awrite(
        self,
        file_path: str,
        content: str,
    ) -> WriteResult:
        """Write content to a new file. Fails if the file already exists."""
        rel = self._to_relative(file_path)
        # Check existence first — protocol requires failure if file exists.
        check = await self._sandbox.run(
            command=["test", "-f", rel],
            timeout_seconds=5,
        )
        if check.exit_code == 0:
            return WriteResult(error=f"File already exists: {file_path}")

        await self._sandbox.write_file(rel, content)
        abs_path = f"{self._workspace}/{rel}"
        return WriteResult(path=abs_path)

    # ── edit ─────────────────────────────────────────────────────────────────

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Perform exact string replacement in an existing file.

        ``old_string`` must appear exactly once unless ``replace_all=True``.
        """
        rel = self._to_relative(file_path)
        raw = await self._sandbox.read_file(rel)
        if raw == "":
            # Could be missing or binary.
            check = await self._sandbox.run(
                command=["test", "-f", rel],
                timeout_seconds=5,
            )
            if check.exit_code != 0:
                return EditResult(error=f"File not found: {file_path}")
            return EditResult(error=f"File is empty or binary: {file_path}")

        occurrences = raw.count(old_string)
        if occurrences == 0:
            return EditResult(error=f"old_string not found in {file_path}")
        if not replace_all and occurrences > 1:
            return EditResult(
                error=f"old_string appears {occurrences} times in {file_path}; "
                "expected exactly 1. Use replace_all=True to replace all."
            )

        new_content = (
            raw.replace(old_string, new_string)
            if replace_all
            else raw.replace(old_string, new_string, 1)
        )
        await self._sandbox.write_file(rel, new_content)
        abs_path = f"{self._workspace}/{rel}"
        count = occurrences if replace_all else 1
        return EditResult(path=abs_path, occurrences=count)

    # ── grep ─────────────────────────────────────────────────────────────────

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Search for a literal substring in files.

        Uses ``grep -rHnF`` (fixed-string, recursive, with filenames and
        line numbers). The protocol requires literal substring matching,
        NOT regex.
        """
        search_dir = self._to_relative(path) if path else "."
        if not search_dir:
            search_dir = "."
        cmd = ["grep", "-rHnF", pattern, search_dir]
        if glob:
            cmd = ["grep", "-rHnF", "--include", glob, pattern, search_dir]

        result = await self._sandbox.run(command=cmd, timeout_seconds=30)

        if result.exit_code not in (0, 1):
            # exit_code=0: matches found; 1: no matches; other: error
            return GrepResult(error=f"grep failed: {result.stderr.strip()}")

        matches: list[GrepMatch] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            # grep -Hn format: path:line_number:text
            parts = line.split(":", 2)
            if len(parts) >= 3:
                rel_path = parts[0]
                try:
                    line_no = int(parts[1])
                except ValueError:
                    continue
                text = parts[2]
                # Make path absolute for DeepAgents
                abs_path = f"{self._workspace}/{rel_path}"
                matches.append(GrepMatch(path=abs_path, line=line_no, text=text))

        return GrepResult(matches=matches)

    # ── glob ─────────────────────────────────────────────────────────────────

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        """Find files matching a glob pattern using ``find``."""
        search_dir = self._to_relative(path) if path != "/" else "."
        if not search_dir:
            search_dir = "."
        # Use python's glob module via sandbox for reliable glob support.
        script = (
            "import glob as g, os, json, sys\n"
            "pattern = sys.argv[1]\n"
            "root = sys.argv[2] if len(sys.argv) > 2 else '.'\n"
            "full = os.path.join(root, pattern) if root != '.' else pattern\n"
            "matches = g.glob(full, recursive=True)\n"
            "entries = []\n"
            "for m in matches:\n"
            "    rel = os.path.relpath(m, root) if root != '.' else m\n"
            "    entries.append({'path': rel, 'is_dir': os.path.isdir(m)})\n"
            "print(json.dumps(entries))\n"
        )
        result = await self._sandbox.run(
            command=["python3", "-c", script, pattern, search_dir],
            timeout_seconds=15,
        )
        if result.exit_code != 0:
            return GlobResult(
                error=f"glob failed: {result.stderr.strip() or result.stdout.strip()}"
            )

        import json

        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError:
            return GlobResult(error=f"glob: invalid JSON output: {result.stdout[:200]}")

        entries: list[FileInfo] = []
        for item in raw:
            rel_path = item["path"]
            abs_path = f"{self._workspace}/{rel_path}"
            entries.append(FileInfo(path=abs_path, is_dir=item.get("is_dir", False)))

        return GlobResult(matches=entries)

    # ── execute ──────────────────────────────────────────────────────────────

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a shell command via ``sandbox.run(["sh", "-c", command])``."""
        result = await self._sandbox.run(
            command=["sh", "-c", command],
            timeout_seconds=timeout or 60,
        )
        combined = result.stdout
        if result.stderr:
            combined = f"{combined}\n{result.stderr}" if combined else result.stderr

        return ExecuteResponse(
            output=combined,
            exit_code=result.exit_code,
            truncated=False,
        )

    # ── upload / download files ──────────────────────────────────────────────

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload files by writing them through the sandbox."""
        responses: list[FileUploadResponse] = []
        for path, content_bytes in files:
            rel = self._to_relative(path)
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
                continue
            await self._sandbox.write_file(rel, content)
            responses.append(FileUploadResponse(path=path))
        return responses

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download files by reading them through the sandbox."""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            rel = self._to_relative(path)
            raw = await self._sandbox.read_file(rel)
            if raw == "":
                check = await self._sandbox.run(
                    command=["test", "-f", rel],
                    timeout_seconds=5,
                )
                if check.exit_code != 0:
                    responses.append(FileDownloadResponse(path=path, error="file_not_found"))
                    continue
            responses.append(FileDownloadResponse(path=path, content=raw.encode("utf-8")))
        return responses


__all__ = ["SandboxBackendAdapter"]
