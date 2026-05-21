"""FakeSandboxAdapter — tempdir-backed sandbox for unit tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openbot.application.ports.sandbox import ExecResult
from openbot.infrastructure.sandboxes.fake import FakeSandboxAdapter


@pytest.fixture
def repo_url(tmp_path: Path) -> str:
    """Build a tiny local git repo we can clone from.

    Returns a ``file://`` URL pointing at a bare clone so tests can
    actually exercise ``git clone`` + ``git push`` without network.
    """
    work = tmp_path / "work"
    work.mkdir()
    (work / "README.md").write_text("hello\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(work),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "add",
            ".",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(work),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return f"file://{bare}"


@pytest.mark.asyncio
async def test_close_removes_workspace() -> None:
    sb = FakeSandboxAdapter()
    workspace = sb.workspace
    assert Path(workspace).exists()
    await sb.close()
    assert not Path(workspace).exists()


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    sb = FakeSandboxAdapter()
    await sb.close()
    # Second close must not raise even though the dir is gone — the
    # use case's outer try/finally may call close twice if cleanup races.
    await sb.close()


@pytest.mark.asyncio
async def test_clone_checks_out_ref(repo_url: str) -> None:
    sb = FakeSandboxAdapter()
    try:
        await sb.clone(repo_url=repo_url, ref="main", token="ignored")
        assert (Path(sb.workspace) / "README.md").read_text() == "hello\n"
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_read_file_returns_text(repo_url: str) -> None:
    sb = FakeSandboxAdapter()
    try:
        await sb.clone(repo_url=repo_url, ref="main", token="x")
        assert await sb.read_file("README.md") == "hello\n"
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_read_missing_file_returns_empty(repo_url: str) -> None:
    sb = FakeSandboxAdapter()
    try:
        await sb.clone(repo_url=repo_url, ref="main", token="x")
        assert await sb.read_file("nope.txt") == ""
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_write_file_creates_parents(repo_url: str) -> None:
    sb = FakeSandboxAdapter()
    try:
        await sb.clone(repo_url=repo_url, ref="main", token="x")
        await sb.write_file("a/b/c.txt", "yo\n")
        assert (Path(sb.workspace) / "a/b/c.txt").read_text() == "yo\n"
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_list_files_respects_max(repo_url: str) -> None:
    sb = FakeSandboxAdapter()
    try:
        await sb.clone(repo_url=repo_url, ref="main", token="x")
        for i in range(7):
            await sb.write_file(f"d/f{i}.txt", str(i))
        files = await sb.list_files(path="d", max=3)
        assert len(files) == 3
        assert all(p.startswith("d/") for p in files)
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_list_files_skips_git_internals(repo_url: str) -> None:
    sb = FakeSandboxAdapter()
    try:
        await sb.clone(repo_url=repo_url, ref="main", token="x")
        files = await sb.list_files()
        assert "README.md" in files
        assert not any(p.startswith(".git/") for p in files)
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_run_returns_exec_result() -> None:
    sb = FakeSandboxAdapter()
    try:
        result = await sb.run(command=["python", "-c", "print('hi')"])
        assert isinstance(result, ExecResult)
        assert result.exit_code == 0
        assert "hi" in result.stdout
        assert result.timed_out is False
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_run_captures_nonzero_exit() -> None:
    sb = FakeSandboxAdapter()
    try:
        result = await sb.run(command=["python", "-c", "import sys; sys.exit(3)"])
        assert result.exit_code == 3
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_run_times_out() -> None:
    sb = FakeSandboxAdapter()
    try:
        result = await sb.run(
            command=["python", "-c", "import time; time.sleep(5)"],
            timeout_seconds=1,
        )
        assert result.timed_out is True
        # ``exit_code`` for a forced kill is runtime-specific; we only
        # assert it's non-zero so behavior stays portable across OSes.
        assert result.exit_code != 0
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_git_diff_returns_working_tree_diff(repo_url: str) -> None:
    sb = FakeSandboxAdapter()
    try:
        await sb.clone(repo_url=repo_url, ref="main", token="x")
        await sb.write_file("README.md", "hello world\n")
        diff = await sb.git_diff()
        assert "hello world" in diff
        assert "README.md" in diff
    finally:
        await sb.close()


@pytest.mark.asyncio
async def test_commit_and_push_creates_branch_on_origin(repo_url: str) -> None:
    sb = FakeSandboxAdapter()
    try:
        await sb.clone(repo_url=repo_url, ref="main", token="x")
        await sb.write_file("README.md", "edited\n")
        await sb.commit_and_push(
            branch_ref="openbot/fix-test",
            message="test fix",
            token="ignored-for-file-url",
        )
        # Confirm: bare repo now has the new ref.
        out = subprocess.run(
            [
                "git",
                "ls-remote",
                repo_url.removeprefix("file://"),
                "refs/heads/openbot/fix-test",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "refs/heads/openbot/fix-test" in out.stdout
    finally:
        await sb.close()
