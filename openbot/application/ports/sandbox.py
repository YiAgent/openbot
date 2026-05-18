"""SandboxPort — isolated execution surface for agent tools.

Defined for the agent slice. The first impl will live under
``infrastructure/agents/`` and adapt the chosen runtime (DeepAgents +
Daytona/Modal/Docker, per locked-boundary §3).

Note: ``evals.sandboxes.factory`` remains under ``evals/`` per PRD §3.
``SandboxPort`` is the Hexagonal contract for the agent slice; they are
separate concerns.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class SandboxPort(Protocol):
    """Run one command in an isolated environment, return its result."""

    async def run(
        self,
        *,
        command: list[str],
        env: Mapping[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        """Return {'stdout': str, 'stderr': str, 'exit_code': int, 'timed_out': bool}."""
        ...


__all__ = ["SandboxPort"]
