# tests/unit/infrastructure/sandboxes/test_backend_adapter.py
"""Unit tests for SandboxBackendAdapter.

Tests the adapter that wraps SandboxPort as a DeepAgents SandboxBackendProtocol.
Uses FakeSandbox for read/write/edit tests and a TempSandbox (real tempdir + subprocess)
for command-execution tests (ls, grep, glob, execute).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from openbot.application.ports.sandbox import ExecResult
from openbot.infrastructure.sandboxes.backend_adapter import SandboxBackendAdapter
from openbot.testing.fakes.sandbox import FakeSandbox

# ── TempSandbox: a SandboxPort that actually executes commands ──────────────


@dataclass
class TempSandbox:
    """Minimal SandboxPort that runs real commands in a tempdir.

    Only implements the methods the adapter needs: workspace, read_file,
    write_file, run. Other SandboxPort methods are no-ops.
    """

    workspace: str = field(default_factory=lambda: tempfile.mkdtemp(prefix="test-sandbox-"))

    async def read_file(self, path: str) -> str:
        full = os.path.join(self.workspace, path)
        try:
            with open(full) as f:
                return f.read()
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
            return ""

    async def write_file(self, path: str, content: str) -> None:
        full = os.path.join(self.workspace, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)

    async def run(
        self,
        *,
        command: list[str],
        timeout_seconds: int = 60,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            return ExecResult(
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=proc.returncode or 0,
                timed_out=False,
            )
        except TimeoutError:
            proc.kill()
            return ExecResult(stdout="", stderr="timeout", exit_code=-1, timed_out=True)

    async def list_files(self, *, path: str = ".", max: int = 200) -> list[str]:
        return []

    async def git_diff(self) -> str:
        return ""

    async def clone(self, **kwargs: Any) -> None:
        pass

    async def commit_and_push(self, **kwargs: Any) -> None:
        pass

    async def close(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def fake_sandbox() -> FakeSandbox:
    return FakeSandbox(workspace="/workspace")


@pytest.fixture()
def fake_adapter(fake_sandbox: FakeSandbox) -> SandboxBackendAdapter:
    return SandboxBackendAdapter(fake_sandbox)


@pytest.fixture()
def temp_sandbox() -> TempSandbox:
    sb = TempSandbox()
    yield sb  # type: ignore[misc]
    shutil.rmtree(sb.workspace, ignore_errors=True)


@pytest.fixture()
def temp_adapter(temp_sandbox: TempSandbox) -> SandboxBackendAdapter:
    return SandboxBackendAdapter(temp_sandbox)  # type: ignore[arg-type]


# ── id property ──────────────────────────────────────────────────────────────


class TestId:
    def test_returns_sandbox_adapter_with_workspace(
        self, fake_adapter: SandboxBackendAdapter
    ) -> None:
        assert fake_adapter.id == "sandbox-adapter-/workspace"


# ── path mapping ─────────────────────────────────────────────────────────────


class TestPathMapping:
    def test_strips_workspace_prefix(self, fake_adapter: SandboxBackendAdapter) -> None:
        assert fake_adapter._to_relative("/workspace/src/main.py") == "src/main.py"

    def test_strips_leading_slash(self, fake_adapter: SandboxBackendAdapter) -> None:
        assert fake_adapter._to_relative("/other/path.py") == "other/path.py"

    def test_keeps_relative_path(self, fake_adapter: SandboxBackendAdapter) -> None:
        assert fake_adapter._to_relative("relative/path.py") == "relative/path.py"

    def test_strips_workspace_root(self, fake_adapter: SandboxBackendAdapter) -> None:
        assert fake_adapter._to_relative("/workspace") == ""


# ── ls ───────────────────────────────────────────────────────────────────────


class TestLs:
    @pytest.mark.asyncio()
    async def test_returns_entries_with_is_dir(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        ws = sandbox.workspace
        os.makedirs(os.path.join(ws, "subdir"), exist_ok=True)
        with open(os.path.join(ws, "file.txt"), "w") as f:
            f.write("content")

        result = await temp_adapter.als(ws)
        assert result.error is None
        assert result.entries is not None
        names = {e["path"] for e in result.entries}
        assert "file.txt" in names
        assert "subdir" in names
        for entry in result.entries:
            if entry["path"] == "subdir":
                assert entry.get("is_dir") is True
            elif entry["path"] == "file.txt":
                assert entry.get("is_dir") is False

    @pytest.mark.asyncio()
    async def test_ls_nonexistent_dir_returns_error(
        self, temp_adapter: SandboxBackendAdapter
    ) -> None:
        sandbox = temp_adapter._sandbox
        result = await temp_adapter.als(f"{sandbox.workspace}/nonexistent")
        assert result.error is not None

    @pytest.mark.asyncio()
    async def test_ls_relative_path(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        with open(os.path.join(sandbox.workspace, "abc.txt"), "w") as f:
            f.write("content")

        result = await temp_adapter.als(".")
        assert result.error is None
        assert result.entries is not None
        names = {e["path"] for e in result.entries}
        assert "abc.txt" in names


# ── read ─────────────────────────────────────────────────────────────────────


class TestRead:
    @pytest.mark.asyncio()
    async def test_read_formats_with_line_numbers(
        self, fake_adapter: SandboxBackendAdapter
    ) -> None:
        sandbox = fake_adapter._sandbox
        sandbox.fake_files["hello.py"] = "print('hello')\nprint('world')\n"
        result = await fake_adapter.aread("/workspace/hello.py")
        assert result.error is None
        assert result.file_data is not None
        content = result.file_data["content"]
        assert "1\t" in content
        assert "print('hello')" in content
        assert "2\t" in content
        assert "print('world')" in content

    @pytest.mark.asyncio()
    async def test_read_pagination(self, fake_adapter: SandboxBackendAdapter) -> None:
        sandbox = fake_adapter._sandbox
        sandbox.fake_files["big.py"] = "\n".join(f"line {i}" for i in range(100)) + "\n"
        result = await fake_adapter.aread("/workspace/big.py", offset=10, limit=5)
        assert result.error is None
        assert result.file_data is not None
        content = result.file_data["content"]
        assert "11\t" in content
        assert "line 10" in content
        lines = content.strip().split("\n")
        assert len(lines) == 5

    @pytest.mark.asyncio()
    async def test_read_missing_file_returns_error(
        self, temp_adapter: SandboxBackendAdapter
    ) -> None:
        sandbox = temp_adapter._sandbox
        result = await temp_adapter.aread(f"{sandbox.workspace}/nonexistent.py")
        assert result.error is not None
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio()
    async def test_read_empty_file(self, fake_adapter: SandboxBackendAdapter) -> None:
        sandbox = fake_adapter._sandbox
        sandbox.fake_files["empty.txt"] = ""
        result = await fake_adapter.aread("/workspace/empty.txt")
        assert result.error is None
        assert result.file_data is not None
        assert result.file_data["content"] == ""


# ── write ────────────────────────────────────────────────────────────────────


class TestWrite:
    @pytest.mark.asyncio()
    async def test_write_creates_new_file(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        ws = sandbox.workspace
        result = await temp_adapter.awrite(f"{ws}/new.txt", "hello")
        assert result.error is None
        assert result.path == f"{ws}/new.txt"
        content = await sandbox.read_file("new.txt")
        assert content == "hello"

    @pytest.mark.asyncio()
    async def test_write_fails_if_exists(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        ws = sandbox.workspace
        await sandbox.write_file("existing.txt", "old content")
        result = await temp_adapter.awrite(f"{ws}/existing.txt", "new content")
        assert result.error is not None
        assert "already exists" in result.error


# ── edit ─────────────────────────────────────────────────────────────────────


class TestEdit:
    @pytest.mark.asyncio()
    async def test_edit_single_occurrence(self, fake_adapter: SandboxBackendAdapter) -> None:
        sandbox = fake_adapter._sandbox
        sandbox.fake_files["code.py"] = "def foo():\n    return 42\n"
        result = await fake_adapter.aedit("/workspace/code.py", "return 42", "return 43")
        assert result.error is None
        assert result.occurrences == 1
        assert result.path == "/workspace/code.py"
        assert sandbox.fake_files["code.py"] == "def foo():\n    return 43\n"

    @pytest.mark.asyncio()
    async def test_edit_multiple_occurrences_error(
        self, fake_adapter: SandboxBackendAdapter
    ) -> None:
        sandbox = fake_adapter._sandbox
        sandbox.fake_files["dup.py"] = "x = 1\ny = 1\n"
        result = await fake_adapter.aedit("/workspace/dup.py", "1", "2")
        assert result.error is not None
        assert "appears 2 times" in result.error

    @pytest.mark.asyncio()
    async def test_edit_replace_all(self, fake_adapter: SandboxBackendAdapter) -> None:
        sandbox = fake_adapter._sandbox
        sandbox.fake_files["dup.py"] = "x = 1\ny = 1\n"
        result = await fake_adapter.aedit("/workspace/dup.py", "1", "2", replace_all=True)
        assert result.error is None
        assert result.occurrences == 2
        assert sandbox.fake_files["dup.py"] == "x = 2\ny = 2\n"

    @pytest.mark.asyncio()
    async def test_edit_not_found(self, fake_adapter: SandboxBackendAdapter) -> None:
        sandbox = fake_adapter._sandbox
        sandbox.fake_files["code.py"] = "def foo():\n    pass\n"
        result = await fake_adapter.aedit("/workspace/code.py", "nonexistent", "replacement")
        assert result.error is not None
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio()
    async def test_edit_missing_file(self, fake_adapter: SandboxBackendAdapter) -> None:
        result = await fake_adapter.aedit("/workspace/missing.py", "old", "new")
        assert result.error is not None


# ── grep ─────────────────────────────────────────────────────────────────────


class TestGrep:
    @pytest.mark.asyncio()
    async def test_grep_literal_substring(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        ws = sandbox.workspace
        await sandbox.write_file("a.py", "# TODO fix this\ndone\n")

        result = await temp_adapter.agrep("TODO", ws)
        assert result.error is None
        assert result.matches is not None
        assert len(result.matches) >= 1
        assert any("TODO" in m["text"] for m in result.matches)
        assert any(m["path"].startswith(ws + "/") for m in result.matches)

    @pytest.mark.asyncio()
    async def test_grep_no_matches(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        ws = sandbox.workspace
        await sandbox.write_file("b.py", "hello\n")

        result = await temp_adapter.agrep("NONEXISTENT", ws)
        assert result.error is None
        assert result.matches is not None
        assert len(result.matches) == 0

    @pytest.mark.asyncio()
    async def test_grep_with_glob_filter(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        ws = sandbox.workspace
        await sandbox.write_file("c.py", "TODO in py\n")
        await sandbox.write_file("c.txt", "TODO in txt\n")

        result = await temp_adapter.agrep("TODO", ws, glob="*.py")
        assert result.error is None
        assert result.matches is not None
        assert all(m["path"].endswith(".py") for m in result.matches)


# ── glob ─────────────────────────────────────────────────────────────────────


class TestGlob:
    @pytest.mark.asyncio()
    async def test_glob_finds_files(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        ws = sandbox.workspace
        os.makedirs(os.path.join(ws, "src"), exist_ok=True)
        await sandbox.write_file("src/main.py", "print(1)")
        await sandbox.write_file("src/util.py", "print(2)")
        await sandbox.write_file("README.md", "# readme")

        result = await temp_adapter.aglob("src/**/*.py", ws)
        assert result.error is None
        assert result.matches is not None
        names = {m["path"] for m in result.matches}
        assert os.path.join(ws, "src/main.py") in names
        assert os.path.join(ws, "src/util.py") in names
        assert os.path.join(ws, "README.md") not in names

    @pytest.mark.asyncio()
    async def test_glob_no_matches(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        result = await temp_adapter.aglob("*.xyz", sandbox.workspace)
        assert result.error is None
        assert result.matches is not None
        assert len(result.matches) == 0


# ── execute ──────────────────────────────────────────────────────────────────


class TestExecute:
    @pytest.mark.asyncio()
    async def test_execute_runs_command(self, temp_adapter: SandboxBackendAdapter) -> None:
        result = await temp_adapter.aexecute("echo hello")
        assert result.exit_code == 0
        assert "hello" in result.output

    @pytest.mark.asyncio()
    async def test_execute_returns_stderr(self, temp_adapter: SandboxBackendAdapter) -> None:
        result = await temp_adapter.aexecute("echo error >&2")
        assert result.exit_code == 0
        assert "error" in result.output

    @pytest.mark.asyncio()
    async def test_execute_nonzero_exit(self, temp_adapter: SandboxBackendAdapter) -> None:
        result = await temp_adapter.aexecute("exit 1")
        assert result.exit_code == 1

    @pytest.mark.asyncio()
    async def test_execute_with_timeout(self, temp_adapter: SandboxBackendAdapter) -> None:
        result = await temp_adapter.aexecute("echo fast", timeout=5)
        assert result.exit_code == 0
        assert "fast" in result.output


# ── upload / download ────────────────────────────────────────────────────────


class TestUploadDownload:
    @pytest.mark.asyncio()
    async def test_upload_files(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        ws = sandbox.workspace
        responses = await temp_adapter.aupload_files([(f"{ws}/uploaded.txt", b"content here")])
        assert len(responses) == 1
        assert responses[0].error is None
        content = await sandbox.read_file("uploaded.txt")
        assert content == "content here"

    @pytest.mark.asyncio()
    async def test_download_files(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        ws = sandbox.workspace
        await sandbox.write_file("download.txt", "file content")
        responses = await temp_adapter.adownload_files([f"{ws}/download.txt"])
        assert len(responses) == 1
        assert responses[0].error is None
        assert responses[0].content == b"file content"

    @pytest.mark.asyncio()
    async def test_download_missing_file(self, temp_adapter: SandboxBackendAdapter) -> None:
        sandbox = temp_adapter._sandbox
        ws = sandbox.workspace
        responses = await temp_adapter.adownload_files([f"{ws}/missing.txt"])
        assert len(responses) == 1
        assert responses[0].error == "file_not_found"
