"""CancellationPort — cross-dyno run cancellation signal."""

from __future__ import annotations

from typing import Protocol


class CancellationPort(Protocol):
    """Signal/check a cancellation flag durable across dynos."""

    async def signal(self, run_id: str) -> None:
        """Mark run_id as cancelled. Sets Redis flag and cancels local task."""
        ...

    async def is_cancelled(self, run_id: str) -> bool:
        """Cheap point query — returns False on Redis flap (fail-open)."""
        ...
