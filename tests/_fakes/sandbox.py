"""Compat shim — provides FakeSandboxLifecycle for legacy flat test files.

PR #87 moved test fakes to ``openbot.testing.fakes`` using the port-fake
pattern. ``FakeSandboxLifecycle`` was dropped from the new package because
it was used only by the e2e harness and a handful of integration tests.
We restore the minimal lifecycle-only fake here so the remaining flat
test files from main can continue to import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openbot.application.ports.sandbox import CloneStrategy
from openbot.testing.fakes.sandbox import FakeSandbox  # noqa: F401

if TYPE_CHECKING:
    pass


@dataclass
class FakeSandboxLifecycle:
    """Lifecycle-only fake — records ``clone``, ``commit_and_push``, and ``close``."""

    workspace: str = "/workspace/repo"
    cloned: list[tuple[str, str, str]] = field(default_factory=list)
    clone_strategies: list[CloneStrategy] = field(default_factory=list)
    pushed: list[tuple[str, str, str]] = field(default_factory=list)
    closed: bool = False

    async def clone(
        self,
        url: str,
        ref: str,
        token: str | None = None,
        *,
        strategy: CloneStrategy = CloneStrategy.SHALLOW,
    ) -> str:
        # Return the workspace path — tests assert against .cloned / .clone_strategies.
        self.cloned.append((url, ref, token or ""))
        self.clone_strategies.append(strategy)
        return self.workspace

    async def commit_and_push(
        self,
        message: str = "fix",
        token: str | None = None,
        branch_ref: str | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> str:
        self.pushed.append((branch_ref or "", message, token or ""))
        return "abc1234"

    async def __aenter__(self) -> FakeSandboxLifecycle:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def close(self) -> None:
        self.closed = True
