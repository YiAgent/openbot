"""QueuePort — enqueue a parsed event for the worker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from openbot.domain.events import UnifiedEvent
    from openbot.domain.workflows import Feature


@runtime_checkable
class QueuePort(Protocol):
    """Enqueue one event onto the Redis Stream."""

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
        """Returns the Redis stream ID assigned to the entry."""
        ...
