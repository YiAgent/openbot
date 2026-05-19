"""Contract test — FakeRunsRepo conforms to RunsRepoPort structurally."""

from __future__ import annotations

import pytest

from openbot.application.state.runs_repo import TransitionResult
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.intents import EventClassification, Intent, State
from tests._fakes.runs_repo import FakeRunsRepo


def test_fake_has_transition_method() -> None:
    assert callable(getattr(FakeRunsRepo(), "transition", None))


@pytest.mark.asyncio
async def test_queued_result_returned_in_order() -> None:
    repo = FakeRunsRepo()
    repo.queued = [
        TransitionResult(
            classification=EventClassification(
                intent=Intent.START, next_state=State.RUNNING, reason="ok"
            ),
            run_id="r1",
            prev_run_id=None,
            prior_state=State.IDLE,
        )
    ]
    ev = UnifiedEvent(
        kind=EventKind.UNKNOWN,
        channel="github",
        delivery_id="d1",
        repo="owner/repo",
        actor="user1",
    )
    out = await repo.transition(event=ev, new_run_id="r1")
    assert out.run_id == "r1"
    assert repo.calls == [(ev, "r1")]
