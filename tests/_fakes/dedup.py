"""FakeDedup — in-memory DedupPort for tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from openbot.infrastructure.persistence.dedup import DedupOutcome


@dataclass
class FakeDedup:
    """First call per (channel, delivery_id) is FRESH; subsequent are DUPLICATE."""

    seen: set[tuple[str, str]] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def check_and_mark(self, channel: str, delivery_id: str) -> DedupOutcome:
        self.calls.append((channel, delivery_id))
        if not delivery_id:
            return DedupOutcome.FRESH
        key = (channel, delivery_id)
        if key in self.seen:
            return DedupOutcome.DUPLICATE
        self.seen.add(key)
        return DedupOutcome.FRESH
