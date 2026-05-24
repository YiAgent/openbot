"""FakeRunsRepo — in-memory RunsRepoPort.

Records every transition() call as an immutable TransitionRecord tuple.
Responses are injected via the ``responses=[…]`` constructor list; each
call pops the front item. Exhausting the list raises
``IndexError("FakeRunsRepo: responses exhausted")``.

Failure injection: ``fail_after=N`` raises ``fail_with(…)`` once the
combined call count reaches ``N`` (sticky). ``fail_with`` MUST accept a
single string message arg.

All-defaults constructor invariant: ``FakeRunsRepo()`` works with no
args so the module-level ``_PROTOCOL_CHECK`` validates Protocol
conformance at import time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from openbot.application.ports.runs_repo import RunsRepoPort
from openbot.application.state.runs_repo import TransitionResult
from openbot.domain.events import UnifiedEvent

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class TransitionRecord:
    """Snapshot of one transition() call. All fields immutable."""

    event: UnifiedEvent
    new_run_id: str


@dataclass
class FakeRunsRepo:
    """In-memory RunsRepoPort. Construct fresh per test."""

    responses: list[TransitionResult] = field(default_factory=list)
    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _transitions: list[TransitionRecord] = field(default_factory=list, init=False)

    @property
    def transitions(self) -> tuple[TransitionRecord, ...]:
        """All transition() calls in order, as an immutable tuple."""
        return tuple(self._transitions)

    async def transition(self, *, event: UnifiedEvent, new_run_id: str) -> TransitionResult:
        """See :class:`openbot.application.ports.runs_repo.RunsRepoPort`."""
        self._maybe_fail()
        self._transitions.append(TransitionRecord(event=event, new_run_id=new_run_id))
        if not self.responses:
            raise IndexError("FakeRunsRepo: responses exhausted")
        return self.responses.pop(0)

    def _maybe_fail(self) -> None:
        if self.fail_after is not None and len(self._transitions) >= self.fail_after:
            raise self.fail_with("FakeRunsRepo: simulated failure")


# Static check: import-time error if FakeRunsRepo stops satisfying RunsRepoPort.
_PROTOCOL_CHECK: Final[RunsRepoPort] = FakeRunsRepo()


__all__ = ["FakeRunsRepo", "TransitionRecord"]
