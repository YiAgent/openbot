"""DedupPort — atomic delivery dedup contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from openbot.domain.dedup import DedupOutcome


@runtime_checkable
class DedupPort(Protocol):
    """Atomic check-and-mark for (channel, delivery_id) pairs."""

    async def check(self, channel: str, delivery_id: str) -> DedupOutcome:
        """Read-only duplicate check — does NOT mark. Returns FRESH, DUPLICATE, or FALLBACK_OPEN."""
        ...

    async def mark(self, channel: str, delivery_id: str) -> None:
        """Mark a delivery as seen (write). Call after enqueue succeeds."""
        ...

    async def check_and_mark(self, channel: str, delivery_id: str) -> DedupOutcome:
        """Returns FRESH, DUPLICATE, or FALLBACK_OPEN."""
        ...
