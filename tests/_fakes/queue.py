"""FakeQueue — in-memory QueuePort. Each enqueue() returns a monotonic id."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openbot.domain.events import UnifiedEvent
    from openbot.domain.workflows import Feature


@dataclass
class FakeQueue:
    calls: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 0

    async def enqueue(
        self,
        event: UnifiedEvent,
        *,
        feature: Feature,
        task_id: str,
        check_run_id: int | None = None,
        intent: str | None = None,
        run_id: str | None = None,
        prev_run_id: str | None = None,
        resource_key: str | None = None,
        event_seq: int = 0,
    ) -> str:
        self.calls.append(
            {
                "event": event,
                "feature": feature,
                "task_id": task_id,
                "check_run_id": check_run_id,
                "intent": intent,
                "run_id": run_id,
                "prev_run_id": prev_run_id,
                "resource_key": resource_key,
                "event_seq": event_seq,
            }
        )
        sid = f"0-{self.next_id}"
        self.next_id += 1
        return sid
