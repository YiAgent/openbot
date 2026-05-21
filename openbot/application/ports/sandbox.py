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

from openbot.domain.checkout import CloneStrategy


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

    async def clone(
        self,
        *,
        repo_url: str,
        ref: str,
        token: str,
        strategy: CloneStrategy = CloneStrategy.SHALLOW,
    ) -> None:
        """Clone ``repo_url`` at ``ref`` into ``workspace``.

        ``token`` is the short-lived GitHub installation access token —
        adapters interpolate it as ``https://x-access-token:{token}@...``
        if ``repo_url`` is HTTPS. ``file://`` URLs ignore the token.

        ``strategy`` selects the clone depth / filter; the
        ``CheckoutSpec`` produced by ``resolve_checkout`` carries the
        per-workflow choice and the dispatcher passes it through here.
        Adapters MUST honour:

          - ``SHALLOW``        — ``git clone --depth=1 --branch={ref}``
            (default; matches the historical behaviour, smallest payload).
          - ``SHALLOW_HISTORY``— ``git clone --depth=50 --branch={ref}``
            (used by fix when blame / recent log needs a small window).
          - ``BLOBLESS``       — ``git clone --filter=blob:none --no-checkout``
            then ``git checkout {ref}`` (used by review so file content
            comes in lazily, but the full commit graph is available).
          - ``FULL``           — no extra flags (escape hatch; rarely used).

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

    async def commit_and_push(self, *, branch_ref: str, message: str, token: str) -> None:
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
