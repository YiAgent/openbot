"""RunsRepoPort — state-machine CAS write surface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from openbot.application.state.runs_repo import TransitionResult
    from openbot.domain.events import UnifiedEvent


class RunsRepoPort(Protocol):
    """Persisted state machine keyed by `resource_key`."""

    async def transition(self, *, event: UnifiedEvent, new_run_id: str) -> TransitionResult:
        """Classify + CAS-write. See state.runs_repo.transition for semantics."""
        ...
