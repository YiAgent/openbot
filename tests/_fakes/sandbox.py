"""FakeSandbox — programmable SandboxPort that records every command."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeSandbox:
    """Queued-result fake sandbox. Falls back to ``default_result`` when queue is empty."""

    default_result: dict[str, Any] = field(
        default_factory=lambda: {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
        }
    )
    results: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[list[str], Mapping[str, str] | None, int]] = field(default_factory=list)

    async def run(
        self,
        *,
        command: list[str],
        env: Mapping[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        self.calls.append((command, env, timeout_seconds))
        if self.results:
            return self.results.pop(0)
        return dict(self.default_result)


__all__ = ["FakeSandbox"]
