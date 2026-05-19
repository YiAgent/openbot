"""DedupPort — atomic delivery dedup contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from openbot.domain.dedup import DedupOutcome


@runtime_checkable
class DedupPort(Protocol):
    """Atomic check-and-mark for (channel, delivery_id) pairs."""

    async def check_and_mark(self, channel: str, delivery_id: str) -> DedupOutcome:
        """Returns FRESH, DUPLICATE, or FALLBACK_OPEN."""
        ...
