"""FakeQueue — in-memory QueuePort.

Records every enqueue() / enqueue_task_spec() in immutable tuples of
frozen dataclasses. Returns deterministic stream IDs of the form
`"0-<n>"` (mirrors Redis stream-ID shape so caller code parsing IDs
keeps working).

Failure injection: when ``fail_after=N`` is set, the fake raises
``fail_with("FakeQueue: simulated failure")`` once the combined number
of enqueue/enqueue_task_spec calls reaches ``N`` and on every call
thereafter — sticky, not one-shot. ``fail_with`` MUST accept a single
string message arg (true for ``RuntimeError``, ``ValueError``, etc.);
custom exception types with multi-arg constructors will surface a
``TypeError`` at injection time. Both default to "never fail".

Pattern note for the other 11 fakes: this fake instantiates with no
required args so ``_PROTOCOL_CHECK = FakeQueue()`` works at module
import. Every fake in ``openbot.testing.fakes`` MUST keep an
all-defaults constructor for the same reason.
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
        """See :class:`openbot.application.ports.queue.QueuePort`."""
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
        """See :class:`openbot.application.ports.queue.QueuePort`."""
        self._maybe_fail()
        self._task_specs.append(spec)
        return self._next_stream_id()

    def _maybe_fail(self) -> None:
        # Failure budget is shared across both methods — the count is the
        # combined size of _events and _task_specs. Tests that exercise
        # mixed enqueue paths only need to set fail_after once.
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
