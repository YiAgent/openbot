"""FakeCancellation — in-memory CancellationPort."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeCancellation:
    """Records signal() calls; is_cancelled() checks the cancelled set."""

    cancelled: set[str] = field(default_factory=set)
    signal_calls: list[str] = field(default_factory=list)

    async def signal(self, run_id: str) -> None:
        self.signal_calls.append(run_id)
        self.cancelled.add(run_id)

    async def is_cancelled(self, run_id: str) -> bool:
        return run_id in self.cancelled
