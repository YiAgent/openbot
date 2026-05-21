# Slice C — Fix workflow end-to-end (part 2: sandbox port + fake adapter)

> **Continuation of part 1.** Same skill header applies — use
> superpowers:subagent-driven-development or superpowers:executing-plans.

This part holds the single largest commit in slice C — growing the
`SandboxPort` protocol from one method (`run`) to eight, defining the
`ExecResult` value type, and shipping the `FakeSandboxAdapter` so the
new methods have a real implementation as soon as they exist.

**Why these go in one commit:** the protocol claims methods that need
implementations. If we split, import-linter and pytest stay red between
commits (`SandboxPort` consumers can't be type-checked, and the test for
`FakeSandboxAdapter.clone` references a fake that hasn't been written).
Pre-commit hooks would reject either half.

**Source spec:** `docs/superpowers/specs/2026-05-20-fix-deepagent-design.md` §"SandboxPort (existing — grows)".

---

## Task C.3: SandboxPort growth + `FakeSandboxAdapter`

**Files:**
- Modify: `openbot/application/ports/sandbox.py` (1 method → 8 methods + `ExecResult`)
- Create: `openbot/infrastructure/sandboxes/__init__.py`
- Create: `openbot/infrastructure/sandboxes/fake.py`
- Create: `tests/infrastructure/sandboxes/__init__.py`
- Create: `tests/infrastructure/sandboxes/test_fake.py`

### Heads-up: existing `SandboxPort.run` consumers

Before editing the port, sweep the tree for callers that depend on the
old `dict[str, Any]` return shape:

```bash
grep -RIn 'SandboxPort\|sandbox\.run\b' openbot/ tests/ | grep -v _archive
```

Slice A landed `SandboxPort` but no production caller wires it yet. If a
stub call appears in `tests/` or `openbot/` that destructures
`result["stdout"]` / `result["exit_code"]`, update it to read
`.stdout` / `.exit_code` on the new `ExecResult` in this same commit so
import-linter and pytest stay green.

If the grep returns zero non-archive hits, great — the port has been
unused since it landed, and the migration is purely additive.

### Heads-up: do NOT touch `evals/sandboxes/factory.py`

`evals/sandboxes/factory.py` exposes a *separate* `SandboxBackend`
protocol used only by eval suites. Per PRD §3 locked boundaries, the
production fix loop uses `openbot/application/ports/sandbox.SandboxPort`;
the two are intentionally not unified. The port docstring at
`openbot/application/ports/sandbox.py:7` records this — keep that note
intact when rewriting the file.

### Subprocess safety note

`openbot/infrastructure/sandboxes/fake.py` uses
`asyncio.create_subprocess_exec(*argv, ...)` — the **argv-list** form,
never `shell=True` and never a single command string. This is the
recommended safe pattern in Python: no shell interpretation, no
injection risk. The fake never accepts unconstrained user input — argv
is always built from typed parameters (paths the agent already chose,
or hard-coded git subcommands). Bandit clean. Hooks may flag the word
"exec" in `create_subprocess_exec`; that name refers to the underlying
POSIX `execve` family (which is the safe one), not shell-exec.

---

- [ ] **Step 1: Write the failing test file**

```python
# tests/infrastructure/sandboxes/test_fake.py
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
```

- [ ] **Step 2: Verify the tests fail**

```bash
pytest tests/infrastructure/sandboxes/test_fake.py -v
```

Expected: `ImportError: cannot import name 'ExecResult' from 'openbot.application.ports.sandbox'`
plus `ModuleNotFoundError: No module named 'openbot.infrastructure.sandboxes'`.

- [ ] **Step 3: Grow the sandbox port**

Replace `openbot/application/ports/sandbox.py` with:

```python
"""SandboxPort — isolated execution surface for the fix workflow.

Grown in slice C.3 from the single-``run`` port that landed in the agent
slice. The fix loop needs full filesystem access (clone, read, write,
list), a process runner with a structured result, working-tree diff
inspection, and a push step. Each method is intentionally small so
``FakeSandboxAdapter`` (in-process tempdir) and ``DaytonaSandboxAdapter``
(remote workspace) can implement them without leaking transport details.

Note: ``evals.sandboxes.factory.SandboxBackend`` is a *separate* protocol
under ``evals/`` (per PRD §3 locked-boundary). Production fix loop uses
``SandboxPort``; eval suites continue to use ``SandboxBackend``. Do not
cross the import boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Outcome of one process run inside the sandbox.

    ``timed_out`` is True iff the process exceeded ``timeout_seconds``;
    in that case ``exit_code`` is whatever the runtime returned for a
    forced kill (typically -9 / 137). Callers should branch on
    ``timed_out`` first, not on ``exit_code``.
    """

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


@runtime_checkable
class SandboxPort(Protocol):
    """An isolated workspace for one fix attempt.

    Adapters MUST be safe to use across ``async with`` — ``close()`` is
    invoked exactly once per healthy path and must release every
    resource (tempdir, remote workspace, network handle). The
    ``close_is_idempotent`` test for each adapter enforces that a second
    ``close()`` doesn't raise.

    ``workspace`` is the absolute path to the working directory inside
    the sandbox. Tools pass relative paths (``read_file("src/api.py")``);
    the adapter joins against ``workspace`` internally.
    """

    workspace: str

    async def clone(self, *, repo_url: str, ref: str, token: str) -> None:
        """Clone ``repo_url`` at ``ref`` into ``workspace``.

        ``token`` is the short-lived GitHub installation access token —
        adapters interpolate it as ``https://x-access-token:{token}@...``
        if ``repo_url`` is HTTPS. ``file://`` URLs ignore the token.
        Raises on clone failure (network error, bad token, missing ref).
        """
        ...

    async def read_file(self, path: str) -> str:
        """Return UTF-8 text of ``path`` (relative to workspace) or
        ``""`` on missing/binary. Tools don't branch on the failure mode
        — they treat empty as "nothing to read here".
        """
        ...

    async def write_file(self, path: str, content: str) -> None:
        """Write ``content`` (UTF-8) to ``path``. Creates parent dirs.
        Overwrites existing files without prompting.
        """
        ...

    async def list_files(self, *, path: str = ".", max: int = 200) -> list[str]:
        """Recursive listing under ``path`` (workspace-relative). At
        most ``max`` paths returned. Order is stable but unspecified —
        agents should not rely on it for correctness.

        Adapters should skip ``.git/`` internals so agents don't waste
        budget reading object files.
        """
        ...

    async def run(
        self,
        *,
        command: list[str],
        timeout_seconds: int = 60,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        """Run ``command`` (argv form, no shell) in ``workspace``.

        ``env`` extends the adapter's default environment. A
        ``timeout_seconds`` overrun returns ``ExecResult(timed_out=True,
        exit_code=<runtime-specific>)`` rather than raising — callers
        branch on ``timed_out`` first.
        """
        ...

    async def git_diff(self) -> str:
        """Return ``git diff`` of the working tree (uncommitted changes)
        as a unified diff string. Empty string when there are no
        changes.
        """
        ...

    async def commit_and_push(
        self, *, branch_ref: str, message: str, token: str
    ) -> None:
        """Stage all changes, commit with ``message``, push to
        ``branch_ref`` on origin. ``token`` is interpolated into the
        push URL as ``https://x-access-token:{token}@github.com/...``
        (or ignored for ``file://`` origins in tests). Raises on push
        failure.
        """
        ...

    async def close(self) -> None:
        """Release the workspace. Idempotent. Must not raise on the
        second call — the use case's outer try/finally may invoke close
        twice if cleanup races.
        """
        ...


__all__ = ["ExecResult", "SandboxPort"]
```

- [ ] **Step 4: Write the package init files**

```python
# openbot/infrastructure/sandboxes/__init__.py
"""SandboxPort adapters — tempdir-backed fake + Daytona prod impl."""

from openbot.infrastructure.sandboxes.fake import FakeSandboxAdapter

__all__ = ["FakeSandboxAdapter"]
```

```python
# tests/infrastructure/sandboxes/__init__.py
```

(Empty `__init__.py` for the test package.)

- [ ] **Step 5: Write the fake adapter**

The adapter uses `asyncio.create_subprocess_exec(*argv, ...)` — the
safe argv-list form. Never shell=True, never string commands.

```python
# openbot/infrastructure/sandboxes/fake.py
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

    async def commit_and_push(
        self, *, branch_ref: str, message: str, token: str
    ) -> None:
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
                raise RuntimeError(
                    f"{' '.join(argv[:2])} failed: {result.stderr.strip()}"
                )

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
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
            return ExecResult(
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                exit_code=process.returncode if process.returncode is not None else -1,
                timed_out=False,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return ExecResult(
                stdout="",
                stderr=f"timed out after {timeout}s",
                exit_code=process.returncode if process.returncode is not None else -9,
                timed_out=True,
            )


__all__ = ["FakeSandboxAdapter"]
```

- [ ] **Step 6: Verify the tests pass**

```bash
pytest tests/infrastructure/sandboxes/test_fake.py -v
```

Expected: 13 passed.

- [ ] **Step 7: Sweep for old `dict[str, Any]` return-shape consumers**

```bash
grep -RIn 'sandbox\.run\|SandboxPort' openbot/ tests/ | grep -v _archive
```

If any call destructures `result["stdout"]` / `result["exit_code"]` etc.,
update it to read `.stdout` / `.exit_code` from the new `ExecResult` in
this same commit so `make check` stays green. Stay green at commit
boundaries — that's a non-negotiable.

If the grep finds zero non-archive hits beyond the new code added in
this task, the migration is purely additive and no follow-ups are
needed.

- [ ] **Step 8: `make check`**

```bash
make check
```

Expected: all green (formatter, ruff, import-linter contract, full
pytest). If a stub elsewhere broke, fix it now — do not move to the
next task with a red branch.

- [ ] **Step 9: Commit**

```bash
git add openbot/application/ports/sandbox.py \
        openbot/infrastructure/sandboxes/ \
        tests/infrastructure/sandboxes/
git commit -m "feat(fix): slice C.3 — grow SandboxPort + FakeSandboxAdapter"
```

---

**Continue with `2026-05-20-fix-deepagent-slice-c-part3.md`** for tasks
C.4–C.6 (channel adapter additions, Daytona sandbox, fix tools).
