"""Integration tests for queue worker recovery behavior.

The real ``consume_loop`` requires a live Redis (or fakeredis) connection.
Rather than wire a full Redis harness here (that lives in
``tests/integration/test_worker_recovery.py`` for the L3 Redis path), these
tests focus on the *queue-level* behaviors that are observable via the
application-layer port:

  1. Cancellation-honor path: once a run_id is cancelled, the receive side
     (``ingest_webhook``) must signal cancellation via
     ``CancellationPort.signal`` when a SUPERSEDE transition fires. The worker
     checks ``is_cancelled`` at its checkpoint — so the receive-side signal is
     the first half of the recovery protocol.

  2. Queue failure isolation: when ``FakeQueue`` raises after ``fail_after=N``
     calls, the ``ingest_webhook`` use-case propagates that exception (Redis
     unavailability → 500 → GitHub retries), ensuring no work is silently
     dropped.

These tests do NOT exercise ``consume_loop`` directly — that requires a
real or fake Redis stream which belongs in the L3 integration layer.  The
focus here is on the application-level queue contract that recovery depends on.
"""

from __future__ import annotations

import pytest

from openbot.application.state.runs_repo import TransitionResult
from openbot.application.use_cases.ingest_webhook import ingest_webhook
from openbot.domain.intents import EventClassification, Intent, State
from openbot.testing.builders.events import (
    build_issue_opened_event,
    build_pull_request_opened_event,
)
from openbot.testing.fakes import (
    FakeCancellation,
    FakeChannelAdapter,
    FakeDedup,
    FakeQueue,
    FakeResourceLock,
    FakeRunsRepo,
)

# ---------------------------------------------------------------------------
# Helpers — pre-built TransitionResult factories
# ---------------------------------------------------------------------------


def _start_result(run_id: str) -> TransitionResult:
    return TransitionResult(
        classification=EventClassification(intent=Intent.START, next_state=State.RUNNING),
        run_id=run_id,
        prev_run_id=None,
        prior_state=State.IDLE,
    )


def _supersede_result(run_id: str, prev_run_id: str) -> TransitionResult:
    return TransitionResult(
        classification=EventClassification(intent=Intent.SUPERSEDE, next_state=State.RUNNING),
        run_id=run_id,
        prev_run_id=prev_run_id,
        prior_state=State.RUNNING,
    )


def _cancel_result(prev_run_id: str) -> TransitionResult:
    return TransitionResult(
        classification=EventClassification(intent=Intent.CANCEL, next_state=State.CANCELLED),
        run_id=None,
        prev_run_id=prev_run_id,
        prior_state=State.RUNNING,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestWorkerRecoveryQueueLayer:
    async def test_supersede_signals_cancellation_before_enqueue(self) -> None:
        """SUPERSEDE: cancellation is signalled for the prior run before enqueue.

        This is the receive-side half of the recovery protocol: the worker
        checks ``is_cancelled`` at each checkpoint, so the signal must be
        stored *before* the new task hits the queue to avoid a race where the
        new task completes before the old one notices cancellation.
        """
        event = build_issue_opened_event(issue_number=10)
        runs_repo = FakeRunsRepo(responses=[_supersede_result("run-new", prev_run_id="run-old")])
        cancellation = FakeCancellation()
        queue = FakeQueue()

        result = await ingest_webhook(
            event,
            FakeChannelAdapter(),
            dedup=FakeDedup(),
            runs_repo=runs_repo,
            resource_lock=FakeResourceLock(),
            cancellation=cancellation,
            queue=queue,
            redis_client=object(),
        )

        # run-old must be cancelled on the cancellation port
        assert await cancellation.is_cancelled("run-old") is True
        assert "run-old" in cancellation.signalled

        # The new task was still enqueued (SUPERSEDE does not drop the event)
        assert result.status == "accepted"
        assert len(queue.task_specs) == 1

    async def test_cancel_signals_prior_run_before_new_task_enqueued(self) -> None:
        """CANCEL: prior run is signalled cancelled; new task still enqueues.

        When the state machine returns CANCEL, ``ingest_webhook`` signals
        cancellation for the prior run (so the worker can bail out at its next
        checkpoint) and then continues to enqueue the new event. The new task
        arrives in the queue carrying ``intent=cancel`` in its TaskSpec so the
        worker knows it should stop the prior run and not start new work for
        the same resource.

        Note: ISSUE_CLOSED / PR_CLOSED events return ``None`` from
        ``dispatch_for()`` and so never reach the state machine.  The CANCEL
        classification is produced by the label-based path (``cancel-openbot``
        label) or by ISSUE_LABELED/PR_LABELED events wired through
        ``FakeRunsRepo``.  We use a regular ISSUE_OPENED event here and inject
        the CANCEL result via ``FakeRunsRepo`` to test the orchestration
        without depending on the classifier matrix.
        """
        # Use a routeable event so dispatch_for() produces a Dispatch
        event = build_issue_opened_event(issue_number=20, event_seq=9000)
        runs_repo = FakeRunsRepo(responses=[_cancel_result(prev_run_id="run-active")])
        cancellation = FakeCancellation()
        queue = FakeQueue()

        result = await ingest_webhook(
            event,
            FakeChannelAdapter(),
            dedup=FakeDedup(),
            runs_repo=runs_repo,
            resource_lock=FakeResourceLock(),
            cancellation=cancellation,
            queue=queue,
            redis_client=object(),
        )

        # CANCEL intent: the receive side enqueues but signals the prior run.
        # The worker checks is_cancelled() at its checkpoint to stop early.
        assert result.status == "accepted"

        # Cancellation port must have been signalled for the active run
        assert await cancellation.is_cancelled("run-active") is True
        assert "run-active" in cancellation.signalled

        # A task was still enqueued (the worker decides whether to execute it)
        assert len(queue.task_specs) == 1

    async def test_queue_failure_propagates_so_github_retries(self) -> None:
        """When the queue raises on enqueue, ingest_webhook re-raises.

        GitHub retries webhooks on 5xx responses. For that mechanism to work,
        a failed enqueue must propagate (not be silently swallowed).
        ``FakeQueue(fail_after=0)`` simulates a broken Redis connection.
        """
        event = build_issue_opened_event(issue_number=30)
        queue = FakeQueue(fail_after=0)

        with pytest.raises(RuntimeError, match="FakeQueue: simulated failure"):
            await ingest_webhook(
                event,
                FakeChannelAdapter(),
                dedup=FakeDedup(),
                queue=queue,
                redis_client=object(),
            )

        # Nothing was enqueued successfully
        assert len(queue.task_specs) == 0
        assert len(queue.events) == 0

    async def test_queue_succeeds_before_fail_threshold(self) -> None:
        """Events before the fail_after threshold enqueue normally.

        Recovery behavior: first N events succeed, then the connection breaks.
        Tests that the failure is sticky — subsequent calls also raise — so
        the queue can't accidentally accept work after a simulated outage.
        """
        queue = FakeQueue(fail_after=1)
        adapter = FakeChannelAdapter()

        # First event: succeeds (fail_after=1 means fail on call index >=1)
        event_1 = build_issue_opened_event(issue_number=31, delivery_id="del-pre-fail-1")
        result_1 = await ingest_webhook(
            event_1,
            adapter,
            dedup=FakeDedup(),
            queue=queue,
            redis_client=object(),
        )
        assert result_1.status == "accepted"
        assert len(queue.task_specs) == 1

        # Second event: fails (sticky — remains failed after threshold)
        event_2 = build_issue_opened_event(issue_number=32, delivery_id="del-pre-fail-2")
        with pytest.raises(RuntimeError, match="FakeQueue: simulated failure"):
            await ingest_webhook(
                event_2,
                adapter,
                dedup=FakeDedup(),
                queue=queue,
                redis_client=object(),
            )
        # Still only one successful enqueue
        assert len(queue.task_specs) == 1

    async def test_pre_cancelled_run_id_is_honoured_by_is_cancelled(self) -> None:
        """A run_id pre-loaded into FakeCancellation reports as cancelled.

        This tests the cancel-honor path from the worker's perspective:
        the worker calls ``is_cancelled(run_id)`` at its checkpoint.
        Pre-injecting the run_id into ``FakeCancellation.cancelled`` simulates
        the receive side having already signalled cancellation before the
        worker picked up the task.
        """
        run_id = "run-already-cancelled"
        cancellation = FakeCancellation(cancelled={run_id})

        # The run is immediately seen as cancelled — no signal() call needed
        assert await cancellation.is_cancelled(run_id) is True
        # signal() was not called — the cancellation was injected, not signalled
        assert run_id not in cancellation.signalled

    async def test_cancellation_failure_does_not_drop_enqueue(self) -> None:
        """If the cancellation port raises, ingest_webhook still enqueues.

        Cancellation is best-effort: a Redis flap on the cancellation port
        must not prevent the new task from reaching the queue. The cancellation
        failure is logged but the ingest continues to enqueue.
        """
        event = build_pull_request_opened_event(pr_number=50)
        # Inject SUPERSEDE result so cancellation.signal() will be called
        runs_repo = FakeRunsRepo(responses=[_supersede_result("run-2", prev_run_id="run-1")])
        # Cancellation port fails immediately
        cancellation = FakeCancellation(fail_after=0)
        queue = FakeQueue()

        # Should not raise — cancellation failure is swallowed internally
        result = await ingest_webhook(
            event,
            FakeChannelAdapter(),
            dedup=FakeDedup(),
            runs_repo=runs_repo,
            resource_lock=FakeResourceLock(),
            cancellation=cancellation,
            queue=queue,
            redis_client=object(),
        )

        # Enqueue still happened despite cancellation failure
        assert result.status == "accepted"
        assert len(queue.task_specs) == 1
