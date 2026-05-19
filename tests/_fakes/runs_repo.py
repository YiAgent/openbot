"""FakeRunsRepo — programmable TransitionResult queue."""

from __future__ import annotations

from dataclasses import dataclass, field

from openbot.application.state.runs_repo import TransitionResult
from openbot.domain.events import UnifiedEvent


@dataclass
class FakeRunsRepo:
    queued: list[TransitionResult] = field(default_factory=list)
    calls: list[tuple[UnifiedEvent, str]] = field(default_factory=list)

    async def transition(self, *, event: UnifiedEvent, new_run_id: str) -> TransitionResult:
        self.calls.append((event, new_run_id))
        if not self.queued:
            raise AssertionError("FakeRunsRepo: no result queued for transition()")
        return self.queued.pop(0)
