"""Contract tests for RunsRepoPort.

Both [fake] and [real] parametrizations run the same assertions.

Fake side: FakeRunsRepo with pre-loaded responses (it pops from the list).
Real side: SqlRunsRepo backed by an in-memory aiosqlite database.

The fake fixture tailors its responses per test via request.node.name so each
test's assertions hold for both implementations.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openbot.application.ports.runs_repo import RunsRepoPort
from openbot.application.state.runs_repo import TransitionResult
from openbot.domain.intents import EventClassification, Intent, State
from openbot.infrastructure.persistence.runs_repo_impl import SqlRunsRepo
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes.runs_repo import FakeRunsRepo
from openbot.testing.inmemory.postgres import build_inmemory_db


def _start_result(run_id: str, prev_run_id: str | None = None) -> TransitionResult:
    return TransitionResult(
        classification=EventClassification(intent=Intent.START, next_state=State.RUNNING),
        run_id=run_id,
        prev_run_id=prev_run_id,
        prior_state=State.IDLE,
    )


def _supersede_result(run_id: str, prev_run_id: str) -> TransitionResult:
    return TransitionResult(
        classification=EventClassification(intent=Intent.SUPERSEDE, next_state=State.RUNNING),
        run_id=run_id,
        prev_run_id=prev_run_id,
        prior_state=State.RUNNING,
    )


def _fake_responses_for(test_name: str) -> list[TransitionResult]:
    """Return the minimal response list for the named test."""
    if "first_event" in test_name:
        # 1 transition call
        return [_start_result("r1")]
    if "supersedes" in test_name:
        # 2 transition calls: first discarded, second asserted
        return [_start_result("r1"), _supersede_result("r2", prev_run_id="r1")]
    if "distinct_resource" in test_name:
        # 2 transition calls on different resources: both should have prev_run_id=None
        return [_start_result("r1"), _start_result("b")]
    # Fallback: provide three generic START responses
    return [_start_result("r1"), _start_result("r2"), _start_result("b")]


@pytest.fixture(params=["fake", "real"])
async def runs_repo(request: pytest.FixtureRequest) -> AsyncIterator[RunsRepoPort]:
    if request.param == "fake":
        responses = _fake_responses_for(request.node.name)
        yield FakeRunsRepo(responses=responses)
    else:
        async with build_inmemory_db() as sf:
            yield SqlRunsRepo(session_factory=sf)


@pytest.mark.contract
class TestRunsRepoContract:
    async def test_first_event_creates_run(self, runs_repo: RunsRepoPort) -> None:
        event = build_issue_opened_event(issue_number=1)
        result = await runs_repo.transition(event=event, new_run_id="r1")
        assert result.run_id == "r1"
        assert result.prev_run_id is None

    async def test_second_event_supersedes(self, runs_repo: RunsRepoPort) -> None:
        # First call: open issue → START, stores run_id "r1"
        event_open = build_issue_opened_event(issue_number=2)
        await runs_repo.transition(event=event_open, new_run_id="r1")
        # Build a chat mention event that triggers SUPERSEDE on a running resource
        import uuid

        from openbot.domain.events import EventKind, UnifiedEvent

        supersede_event = UnifiedEvent(
            channel="github",
            delivery_id=str(uuid.uuid4()),
            kind=EventKind.ISSUE_COMMENT_CREATED,
            repo="owner/repo",
            actor="octocat",
            issue_number=2,
            comment_body="@openbot fix this",
            actor_type="User",
            installation_id=100,
            event_seq=0,
            raw={},
        )
        result = await runs_repo.transition(event=supersede_event, new_run_id="r2")
        assert result.run_id == "r2"
        assert result.prev_run_id == "r1"

    async def test_distinct_resource_keys_are_independent(self, runs_repo: RunsRepoPort) -> None:
        event_a = build_issue_opened_event(issue_number=10)
        event_b = build_issue_opened_event(issue_number=20)
        result_a = await runs_repo.transition(event=event_a, new_run_id="r1")
        result_b = await runs_repo.transition(event=event_b, new_run_id="b")
        assert result_a.prev_run_id is None
        assert result_b.prev_run_id is None
