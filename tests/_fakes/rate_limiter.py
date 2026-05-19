"""FakeRateLimiter — counts calls per key, deterministic allow/deny."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class FakeRateLimiter:
    """Per-key call counter; returns False after the per-key allowance is exhausted."""

    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    calls: list[tuple[str, int, int]] = field(default_factory=list)

    async def check(self, key: str, *, limit: int, window_seconds: int) -> bool:
        self.calls.append((key, limit, window_seconds))
        self.counts[key] += 1
        return self.counts[key] <= limit


__all__ = ["FakeRateLimiter"]
