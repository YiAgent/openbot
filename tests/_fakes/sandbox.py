"""Sandbox fakes — two programmable SandboxPort stand-ins for different test layers.

FakeSandbox
    Implements the *command-execution* face (``run`` + queued results).
    Used by agent tool tests and the sandbox-port contract suite.

FakeSandboxLifecycle
    Implements the *lifecycle* face (``clone`` + ``commit_and_push`` + ``close``).
    Used by fix-workflow use case tests and E2E fix-loop demos.
    Each call is recorded as a tuple so tests can assert on the exact
    contract the use case must honour against the real DaytonaSandboxAdapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from openbot.application.ports.sandbox import ExecResult


@dataclass
class FakeSandbox:
    """Queued-result fake sandbox. Falls back to ``default_result`` when queue is empty."""

    default_result: ExecResult = field(
        default_factory=lambda: ExecResult(stdout="", stderr="", exit_code=0, timed_out=False)
    )
    results: list[ExecResult] = field(default_factory=list)
    calls: list[tuple[list[str], Mapping[str, str] | None, int]] = field(default_factory=list)

    async def run(
        self,
        *,
        command: list[str],
        env: Mapping[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> ExecResult:
        self.calls.append((command, env, timeout_seconds))
        if self.results:
            return self.results.pop(0)
        return self.default_result


@dataclass
class FakeSandboxLifecycle:
    """Lifecycle-only fake — records ``clone``, ``commit_and_push``, and ``close`` calls.

    Implements only the methods reachable in fix-workflow tests when the
    agent path is monkeypatched. The command-execution surface (``run``,
    ``read_file``, ``write_file``, ``list_files``, ``git_diff``) is
    intentionally omitted; extend or subclass if a test exercises it.

    Token injection is the *adapter's* concern (see
    ``DaytonaSandboxAdapter._inject_token`` + ``SandboxPort`` docstring);
    the use case passes raw URL + token unchanged, so tests assert on
    raw values, not pre-injected URLs.
    """

    workspace: str = "/workspace/repo"
    cloned: list[tuple[str, str, str]] = field(default_factory=list)
    pushed: list[tuple[str, str, str]] = field(default_factory=list)
    closed: bool = False

    async def clone(self, *, repo_url: str, ref: str, token: str) -> None:
        self.cloned.append((repo_url, ref, token))

    async def commit_and_push(self, *, branch_ref: str, message: str, token: str) -> None:
        self.pushed.append((branch_ref, message, token))

    async def close(self) -> None:
        self.closed = True


__all__ = ["FakeSandbox", "FakeSandboxLifecycle"]
