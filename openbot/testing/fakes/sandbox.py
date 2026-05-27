"""FakeSandbox — in-memory SandboxPort.

Implements the full ``SandboxPort`` surface (clone, read_file, write_file,
list_files, run, git_diff, commit_and_push, close).

The primary observation is ``executions: tuple[ExecCall, ...]`` for the
``run()`` surface. Inject results via ``responses=[…]``; each call pops
the front. If the list is empty, ``default_result`` is returned.

``read_file`` / ``list_files`` use ``fake_files: dict[str, str]``.
``write_file`` stores into ``fake_files`` (in-process mutation only).
``git_diff`` returns ``fake_diff``. Lifecycle methods (clone,
commit_and_push, close) are no-ops by default and tracked in
``clone_calls``, ``push_calls``, and ``close_count``.

Failure injection: ``fail_after=N`` raises ``fail_with(…)`` once the
run() call count reaches ``N`` (sticky, run() calls only).

All-defaults constructor: ``FakeSandbox()`` works with no args.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from openbot.application.ports.sandbox import ExecResult, SandboxPort
from openbot.domain.checkout import CloneStrategy


@dataclass(frozen=True)
class ExecCall:
    """Snapshot of one run() call. All fields immutable."""

    command: tuple[str, ...]
    timeout_seconds: int
    env: tuple[tuple[str, str], ...] | None


@dataclass
class FakeSandbox:
    """In-memory SandboxPort. Construct fresh per test."""

    workspace: str = "/tmp/fake-sandbox"
    default_result: ExecResult = field(
        default_factory=lambda: ExecResult(stdout="", stderr="", exit_code=0, timed_out=False)
    )
    responses: list[ExecResult] = field(default_factory=list)
    fake_files: dict[str, str] = field(default_factory=dict)
    fake_diff: str = ""
    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _executions: list[ExecCall] = field(default_factory=list, init=False)
    clone_calls: list[dict[str, str]] = field(default_factory=list, init=False)
    push_calls: list[dict[str, str]] = field(default_factory=list, init=False)
    close_count: int = field(default=0, init=False)

    @property
    def executions(self) -> tuple[ExecCall, ...]:
        """All run() calls in order, as an immutable tuple."""
        return tuple(self._executions)

    async def clone(
        self,
        *,
        repo_url: str,
        ref: str,
        token: str,
        strategy: CloneStrategy = CloneStrategy.SHALLOW,
    ) -> None:
        """See :class:`openbot.application.ports.sandbox.SandboxPort`."""
        self.clone_calls.append({"repo_url": repo_url, "ref": ref, "strategy": strategy.value})

    async def read_file(self, path: str) -> str:
        """Return ``fake_files[path]`` or ``""`` on missing."""
        return self.fake_files.get(path, "")

    async def write_file(self, path: str, content: str) -> None:
        """Store ``content`` in ``fake_files``."""
        self.fake_files[path] = content

    async def list_files(self, *, path: str = ".", max: int = 200) -> list[str]:
        """Return keys from ``fake_files`` that start with ``path``."""
        prefix = "" if path == "." else path.rstrip("/") + "/"
        matches = [k for k in self.fake_files if prefix == "" or k.startswith(prefix)]
        return matches[:max]

    async def run(
        self,
        *,
        command: list[str],
        timeout_seconds: int = 60,
        env: Mapping[str, str] | None = None,
    ) -> ExecResult:
        """See :class:`openbot.application.ports.sandbox.SandboxPort`."""
        self._maybe_fail()
        env_tuple: tuple[tuple[str, str], ...] | None = (
            tuple(sorted(env.items())) if env is not None else None
        )
        self._executions.append(
            ExecCall(command=tuple(command), timeout_seconds=timeout_seconds, env=env_tuple)
        )
        return self.responses.pop(0) if self.responses else self.default_result

    async def git_diff(self) -> str:
        """Return ``fake_diff``."""
        return self.fake_diff

    async def commit_and_push(
        self, *, branch_ref: str, message: str, token: str, force: bool = False
    ) -> None:
        """Record the push call. No-op otherwise."""
        self.push_calls.append({"branch_ref": branch_ref, "message": message})

    async def close(self) -> None:
        """Idempotent no-op. Increments ``close_count``."""
        self.close_count += 1

    def _maybe_fail(self) -> None:
        if self.fail_after is not None and len(self._executions) >= self.fail_after:
            raise self.fail_with("FakeSandbox: simulated failure")


# Static check: import-time error if FakeSandbox stops satisfying SandboxPort.
_PROTOCOL_CHECK: Final[SandboxPort] = FakeSandbox()


__all__ = ["ExecCall", "FakeSandbox"]
