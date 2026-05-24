"""FakeQueue — in-memory QueuePort.

Records every enqueue() / enqueue_task_spec() in immutable tuples of
frozen dataclasses. Returns deterministic stream IDs of the form
`"0-<n>"` (mirrors Redis stream-ID shape so caller code parsing IDs
keeps working).

Failure injection: `fail_after=N` raises `fail_with` on the (N+1)th
enqueue. Both default to "never fail".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from openbot.application.ports.queue import QueuePort
from openbot.domain.events import UnifiedEvent
from openbot.domain.workflows import Feature

if TYPE_CHECKING:
    from openbot.infrastructure.queue.task_spec import TaskSpec


@dataclass(frozen=True)
class EnqueueRecord:
    """Snapshot of one enqueue() call. All fields immutable."""

    event: UnifiedEvent
    feature: Feature
    task_id: str
    check_run_id: int | None
    intent: str | None
    run_id: str | None
    prev_run_id: str | None
    resource_key: str | None
    event_seq: int


@dataclass
class FakeQueue:
    """In-memory QueuePort. Construct fresh per test."""

    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _events: list[EnqueueRecord] = field(default_factory=list, init=False)
    _task_specs: list[TaskSpec] = field(default_factory=list, init=False)
    _next_id: int = field(default=0, init=False)

    @property
    def events(self) -> tuple[EnqueueRecord, ...]:
        """All enqueue() calls in order, as an immutable tuple."""
        return tuple(self._events)

    @property
    def task_specs(self) -> tuple[TaskSpec, ...]:
        """All enqueue_task_spec() calls in order."""
        return tuple(self._task_specs)

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
        self._maybe_fail()
        self._events.append(
            EnqueueRecord(
                event=event,
                feature=feature,
                task_id=task_id,
                check_run_id=check_run_id,
                intent=intent,
                run_id=run_id,
                prev_run_id=prev_run_id,
                resource_key=resource_key,
                event_seq=event_seq,
            )
        )
        return self._next_stream_id()

    async def enqueue_task_spec(self, spec: TaskSpec) -> str:
        self._maybe_fail()
        self._task_specs.append(spec)
        return self._next_stream_id()

    def _maybe_fail(self) -> None:
        used = len(self._events) + len(self._task_specs)
        if self.fail_after is not None and used >= self.fail_after:
            raise self.fail_with("FakeQueue: simulated failure")

    def _next_stream_id(self) -> str:
        sid = f"0-{self._next_id}"
        self._next_id += 1
        return sid


# Static check: import-time error if FakeQueue stops satisfying QueuePort.
_PROTOCOL_CHECK: Final[QueuePort] = FakeQueue()


__all__ = ["EnqueueRecord", "FakeQueue"]
