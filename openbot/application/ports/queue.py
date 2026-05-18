"""QueuePort — enqueue a parsed event for the worker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from openbot.infrastructure.queue.payload import QueuePayload


@runtime_checkable
class QueuePort(Protocol):
    """Enqueue one payload onto the Redis Stream."""

    async def enqueue(self, payload: QueuePayload) -> str:
        """Returns the Redis stream ID assigned to the entry."""
        ...
