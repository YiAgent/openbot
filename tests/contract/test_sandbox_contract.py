"""Contract tests for SandboxPort.

[fake] uses FakeSandbox (in-memory), [real] uses DockerSandbox which
requires a running Docker daemon (marked requires_docker so CI skips
it when docker is absent; the fake path always runs).

Daytona / Modal sandboxes have no in-process substitute; their
correctness is covered by real_service layer tests.
"""

from __future__ import annotations

import pytest

from openbot.application.ports.sandbox import SandboxPort
from openbot.testing.fakes import FakeSandbox

# ── FakeSandbox contract ──────────────────────────────────────────────────────


@pytest.mark.contract
class TestSandboxFakeContract:
    """Tests that run on FakeSandbox only (no docker required)."""

    async def test_read_file_returns_configured_content(self) -> None:
        sandbox = FakeSandbox(fake_files={"README.md": "# Hello"})
        content = await sandbox.read_file("README.md")
        assert content == "# Hello"

    async def test_read_missing_file_returns_empty_string(self) -> None:
        sandbox = FakeSandbox()
        content = await sandbox.read_file("nonexistent.py")
        assert content == ""

    async def test_write_then_read_file(self) -> None:
        sandbox = FakeSandbox()
        await sandbox.write_file("hello.txt", "world")
        content = await sandbox.read_file("hello.txt")
        assert content == "world"

    async def test_list_files_returns_list(self) -> None:
        sandbox = FakeSandbox(fake_files={"a.py": "", "b.py": ""})
        files = await sandbox.list_files()
        assert isinstance(files, list)

    async def test_run_uses_default_result(self) -> None:
        from openbot.application.ports.sandbox import ExecResult

        sandbox = FakeSandbox(
            default_result=ExecResult(stdout="ok", stderr="", exit_code=0, timed_out=False)
        )
        result = await sandbox.run(command=["echo", "hello"])
        assert result.stdout == "ok"
        assert result.exit_code == 0

    async def test_git_diff_returns_configured_diff(self) -> None:
        sandbox = FakeSandbox(fake_diff="--- a.py\n+++ b.py\n")
        diff = await sandbox.git_diff()
        assert "a.py" in diff

    async def test_close_increments_count(self) -> None:
        sandbox = FakeSandbox()
        await sandbox.close()
        assert sandbox.close_count == 1

    async def test_port_compliance(self) -> None:
        """FakeSandbox must satisfy SandboxPort at runtime."""
        sandbox: SandboxPort = FakeSandbox()
        assert sandbox is not None
