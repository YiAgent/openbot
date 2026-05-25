"""Integration tests for ingest_webhook use-case.

Wires FakeDedup + FakeQueue + FakeResourceLock + FakeRunsRepo + FakeCancellation
together through the real ingest_webhook function.  No network, no DB.

Design rule (spec §8.3): integration tests must call *at least two* fake
adapters so they exercise orchestration, not just one port in isolation.
"""

from __future__ import annotations

import pytest

from openbot.application.state.runs_repo import TransitionResult
from openbot.application.use_cases.ingest_webhook import ingest_webhook
from openbot.domain.intents import EventClassification, Intent, State
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes import (
    FakeCancellation,
    FakeChannelAdapter,
    FakeDedup,
    FakeQueue,
    FakeResourceLock,
    FakeRunsRepo,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _ignore_result(run_id: str) -> TransitionResult:
    return TransitionResult(
        classification=EventClassification(
            intent=Intent.IGNORE, next_state=State.RUNNING, reason="already_running"
        ),
        run_id=run_id,
        prev_run_id=None,
        prior_state=State.RUNNING,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIngestWebhookDuplication:
    async def test_duplicate_event_returns_duplicate_status(self) -> None:
        """Dedup marks delivery_id → second call returns 'duplicate'."""
        event = build_issue_opened_event(issue_number=1)
        adapter = FakeChannelAdapter()
        dedup = FakeDedup()
        queue = FakeQueue()

        # First call marks it
        await ingest_webhook(
            event,
            adapter,
            dedup=dedup,
            queue=queue,
            redis_client=object(),
        )
        # Second call with same event → DUPLICATE
        result = await ingest_webhook(
            event,
            adapter,
            dedup=dedup,
            queue=queue,
            redis_client=object(),
        )
        assert result.status == "duplicate"

    async def test_duplicate_event_does_not_enqueue(self) -> None:
        """A duplicate must not touch the queue."""
        event = build_issue_opened_event(issue_number=2)
        adapter = FakeChannelAdapter()
        dedup = FakeDedup()
        queue = FakeQueue()

        # Prime the dedup cache manually
        await dedup.check_and_mark(event.channel, event.delivery_id)
        result = await ingest_webhook(
            event,
            adapter,
            dedup=dedup,
            queue=queue,
            redis_client=object(),
        )
        assert result.status == "duplicate"
        assert len(queue.task_specs) == 0


@pytest.mark.integration
class TestIngestWebhookIgnored:
    async def test_irrelevant_kind_returns_ignored(self) -> None:
        """Events with no dispatch route return 'ignored'."""
        import uuid

        from openbot.domain.events import EventKind, UnifiedEvent

        # A PUSH event has no handler
        push_event = UnifiedEvent(
            channel="github",
            delivery_id=str(uuid.uuid4()),
            kind=EventKind.UNKNOWN,
            repo="owner/repo",
            actor="octocat",
            issue_number=None,
            comment_body=None,
            actor_type="User",
            installation_id=100,
            event_seq=0,
            raw={},
        )
        adapter = FakeChannelAdapter()
        dedup = FakeDedup()
        queue = FakeQueue()

        result = await ingest_webhook(
            push_event,
            adapter,
            dedup=dedup,
            queue=queue,
            redis_client=object(),
        )
        assert result.status == "ignored"

    async def test_ignored_event_does_not_enqueue(self) -> None:
        """Ignored events must not enqueue anything."""
        import uuid

        from openbot.domain.events import EventKind, UnifiedEvent

        push_event = UnifiedEvent(
            channel="github",
            delivery_id=str(uuid.uuid4()),
            kind=EventKind.UNKNOWN,
            repo="owner/repo",
            actor="octocat",
            issue_number=None,
            comment_body=None,
            actor_type="User",
            installation_id=100,
            event_seq=0,
            raw={},
        )
        adapter = FakeChannelAdapter()
        dedup = FakeDedup()
        queue = FakeQueue()

        await ingest_webhook(
            push_event,
            adapter,
            dedup=dedup,
            queue=queue,
            redis_client=object(),
        )
        assert len(queue.task_specs) == 0


@pytest.mark.integration
class TestIngestWebhookAccepted:
    async def test_accepted_event_returns_accepted_status(self) -> None:
        """Relevant, fresh events return 'accepted'."""
        event = build_issue_opened_event(issue_number=5)
        result = await ingest_webhook(
            event,
            FakeChannelAdapter(),
            dedup=FakeDedup(),
            queue=FakeQueue(),
            redis_client=object(),
        )
        assert result.status == "accepted"

    async def test_accepted_event_enqueues_task_spec(self) -> None:
        """Accepted events enqueue exactly one TaskSpec."""
        event = build_issue_opened_event(issue_number=6)
        queue = FakeQueue()
        await ingest_webhook(
            event,
            FakeChannelAdapter(),
            dedup=FakeDedup(),
            queue=queue,
            redis_client=object(),
        )
        assert len(queue.task_specs) == 1

    async def test_accepted_result_has_entry_id(self) -> None:
        """entry_id (Redis stream ID) is set on accepted results."""
        event = build_issue_opened_event(issue_number=7)
        result = await ingest_webhook(
            event,
            FakeChannelAdapter(),
            dedup=FakeDedup(),
            queue=FakeQueue(),
            redis_client=object(),
        )
        assert result.entry_id is not None

    async def test_accepted_result_has_feature(self) -> None:
        """Accepted results carry the feature name derived from the event."""
        event = build_issue_opened_event(issue_number=8)
        result = await ingest_webhook(
            event,
            FakeChannelAdapter(),
            dedup=FakeDedup(),
            queue=FakeQueue(),
            redis_client=object(),
        )
        assert result.feature is not None

    async def test_no_redis_client_raises(self) -> None:
        """Missing redis_client must raise RuntimeError (not silently drop)."""
        event = build_issue_opened_event(issue_number=9)
        with pytest.raises(RuntimeError, match="Redis"):
            await ingest_webhook(
                event,
                FakeChannelAdapter(),
                dedup=FakeDedup(),
                redis_client=None,
            )


@pytest.mark.integration
class TestIngestWebhookStateMachine:
    async def test_state_machine_start_returns_accepted(self) -> None:
        """With runs_repo + resource_lock wired, START intent → 'accepted'."""
        event = build_issue_opened_event(issue_number=10)
        runs_repo = FakeRunsRepo(responses=[_start_result("r1")])
        result = await ingest_webhook(
            event,
            FakeChannelAdapter(),
            dedup=FakeDedup(),
            runs_repo=runs_repo,
            resource_lock=FakeResourceLock(),
            queue=FakeQueue(),
            redis_client=object(),
        )
        assert result.status == "accepted"

    async def test_state_machine_ignore_returns_ignored(self) -> None:
        """With runs_repo + resource_lock, IGNORE intent → 'ignored' (no enqueue)."""
        event = build_issue_opened_event(issue_number=11)
        runs_repo = FakeRunsRepo(responses=[_ignore_result("r1")])
        queue = FakeQueue()
        result = await ingest_webhook(
            event,
            FakeChannelAdapter(),
            dedup=FakeDedup(),
            runs_repo=runs_repo,
            resource_lock=FakeResourceLock(),
            queue=queue,
            redis_client=object(),
        )
        assert result.status == "ignored"
        assert len(queue.task_specs) == 0

    async def test_supersede_signals_prior_cancellation(self) -> None:
        """SUPERSEDE with prev_run_id must signal cancellation for the old run."""
        event = build_issue_opened_event(issue_number=12)
        runs_repo = FakeRunsRepo(responses=[_supersede_result("r2", prev_run_id="r1")])
        cancellation = FakeCancellation()
        await ingest_webhook(
            event,
            FakeChannelAdapter(),
            dedup=FakeDedup(),
            runs_repo=runs_repo,
            resource_lock=FakeResourceLock(),
            cancellation=cancellation,
            queue=FakeQueue(),
            redis_client=object(),
        )
        assert await cancellation.is_cancelled("r1") is True

    async def test_state_machine_exception_falls_back_to_accepted(self) -> None:
        """When runs_repo.transition raises, ingest falls back → still 'accepted'."""
        event = build_issue_opened_event(issue_number=13)
        # Empty response list → IndexError on pop → triggers except branch
        runs_repo = FakeRunsRepo(responses=[])
        result = await ingest_webhook(
            event,
            FakeChannelAdapter(),
            dedup=FakeDedup(),
            runs_repo=runs_repo,
            resource_lock=FakeResourceLock(),
            queue=FakeQueue(),
            redis_client=object(),
        )
        # Falls back to v1 path (no state machine) → accepted
        assert result.status == "accepted"
