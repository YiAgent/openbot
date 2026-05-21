"""FakeSandboxAdapter — tempdir-backed SandboxPort for tests and dev.

Same semantics as Daytona at the port boundary; the implementation just
uses ``tempfile.mkdtemp`` + ``asyncio.create_subprocess_exec`` (the
safe argv-list spawn) so unit tests stay hermetic and fast. The
``commit_and_push`` step shells out to ``git`` (argv form), so the push
target needs to be a reachable URL — tests use ``file://`` origins;
production uses Daytona's adapter, not this one.

Not exposed as a production option. The DI layer (slice C.8) picks
Daytona when ``OPENBOT_SANDBOX_BACKEND=daytona`` (the default per PRD
§3). This adapter exists so use-case tests and E2E demos can exercise
the real port without spinning up a remote workspace.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from openbot.application.ports.sandbox import ExecResult


class FakeSandboxAdapter:
    """In-process SandboxPort backed by ``tempfile.mkdtemp``."""

    def __init__(self) -> None:
        self.workspace: str = tempfile.mkdtemp(prefix="openbot-fix-")
        self._closed: bool = False

    async def clone(self, *, repo_url: str, ref: str, token: str) -> None:
        # Token is unused for file:// origins (tests). Real HTTPS origins
        # would interpolate via x-access-token like the Daytona adapter,
        # but the fake never sees those — production wires Daytona.
        result = await self._run_inside(
            ["git", "clone", "--quiet", "--branch", ref, repo_url, "."],
        )
        if result.exit_code != 0:
            raise RuntimeError(f"clone failed: {result.stderr.strip()}")

    async def read_file(self, path: str) -> str:
        full = Path(self.workspace) / path
        try:
            return full.read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError, IsADirectoryError):
            return ""

    async def write_file(self, path: str, content: str) -> None:
        full = Path(self.workspace) / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    async def list_files(self, *, path: str = ".", max: int = 200) -> list[str]:
        root = Path(self.workspace) / path
        if not root.exists():
            return []
        out: list[str] = []
        for child in sorted(root.rglob("*")):
            if not child.is_file():
                continue
            rel = child.relative_to(self.workspace).as_posix()
            # Skip the .git internals — agents don't need them and
            # listing them blows tool budgets.
            if rel.startswith(".git/") or rel == ".git":
                continue
            out.append(rel)
            if len(out) >= max:
                break
        return out

    async def run(
        self,
        *,
        command: list[str],
        timeout_seconds: int = 60,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        return await self._run_inside(command, timeout=timeout_seconds, env=env)

    async def git_diff(self) -> str:
        result = await self._run_inside(["git", "diff", "--no-color"])
        return result.stdout

    async def commit_and_push(self, *, branch_ref: str, message: str, token: str) -> None:
        # Tests use file:// origins where the token is unused; production
        # uses Daytona which has its own commit_and_push (see C.5).
        steps: tuple[list[str], ...] = (
            [
                "git",
                "-c",
                "user.email=openbot@example.invalid",
                "-c",
                "user.name=OpenBot",
                "checkout",
                "-b",
                branch_ref,
            ],
            ["git", "add", "-A"],
            [
                "git",
                "-c",
                "user.email=openbot@example.invalid",
                "-c",
                "user.name=OpenBot",
                "commit",
                "-q",
                "-m",
                message,
            ],
            ["git", "push", "-q", "origin", branch_ref],
        )
        for argv in steps:
            result = await self._run_inside(argv)
            if result.exit_code != 0:
                raise RuntimeError(f"{' '.join(argv[:2])} failed: {result.stderr.strip()}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self.workspace, ignore_errors=True)

    async def _run_inside(
        self,
        argv: list[str],
        *,
        timeout: int = 60,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        # Argv-form spawn: no shell, no injection surface. The argv
        # list is always assembled from typed parameters above.
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.workspace,
            env={**os.environ, **(env or {})},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return ExecResult(
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                exit_code=process.returncode if process.returncode is not None else -1,
                timed_out=False,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return ExecResult(
                stdout="",
                stderr=f"timed out after {timeout}s",
                exit_code=process.returncode if process.returncode is not None else -9,
                timed_out=True,
            )


__all__ = ["FakeSandboxAdapter"]
