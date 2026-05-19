"""FakeResourceLock — no-op CM that records acquisitions."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class FakeResourceLock:
    """Records lock() calls; always yields acquire_result."""

    calls: list[tuple[str, int]] = field(default_factory=list)
    acquire_result: bool = True

    @asynccontextmanager
    async def lock(self, resource_key: str, *, ttl_seconds: int = 10):
        self.calls.append((resource_key, ttl_seconds))
        yield self.acquire_result
