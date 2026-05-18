"""FakeQueue — in-memory QueuePort. Each enqueue() returns a monotonic id."""

from __future__ import annotations

from dataclasses import dataclass, field

from openbot.infrastructure.queue.payload import QueuePayload


@dataclass
class FakeQueue:
    entries: list[QueuePayload] = field(default_factory=list)
    next_id: int = 0

    async def enqueue(self, payload: QueuePayload) -> str:
        self.entries.append(payload)
        sid = f"0-{self.next_id}"
        self.next_id += 1
        return sid
