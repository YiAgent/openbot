# openbot/application/ports/queue.py
"""QueuePort — enqueue events and task specs for the worker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from openbot.domain.events import UnifiedEvent
    from openbot.domain.workflows import Feature
    from openbot.infrastructure.queue.task_spec import TaskSpec


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
        """Returns the Redis stream entry ID (v1/v2 QueuePayload path)."""
        ...

    async def enqueue_task_spec(self, spec: TaskSpec) -> str:
        """Enqueue a pre-built TaskSpec v3.

        Returns the Redis stream entry ID. Used by decide_and_enqueue()
        to push a fully-decided TaskSpec to the worker queue.
        """
        ...
