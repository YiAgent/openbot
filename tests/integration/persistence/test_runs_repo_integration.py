"""Integration tests for SqlRunsRepo backed by an in-memory aiosqlite DB.

Uses ``build_inmemory_db()`` as an async context manager so every test
gets a fresh, isolated schema — no shared state between tests.

Design rule: these tests exercise the real ``SqlRunsRepo`` (and the
``transition()`` pure function underneath it) through an aiosqlite DB.
No fakes are used for the persistence layer; fakes exist at the port
level for use-case tests, not here.
"""

from __future__ import annotations

import pytest

from openbot.application.state.runs_repo import get_state
from openbot.domain.intents import Intent, State
from openbot.infrastructure.persistence.runs_repo_impl import SqlRunsRepo
from openbot.testing.builders.events import (
    build_issue_opened_event,
    build_pull_request_opened_event,
)
from openbot.testing.inmemory.postgres import build_inmemory_db


@pytest.mark.integration
class TestSqlRunsRepoIntegration:
    async def test_first_transition_creates_row(self) -> None:
        """First event → START intent, run_id set, prev_run_id None."""
        async with build_inmemory_db() as sf:
            repo = SqlRunsRepo(session_factory=sf)
            event = build_issue_opened_event(issue_number=1, event_seq=1000)

            result = await repo.transition(event=event, new_run_id="run-001")

            # Classification must be START for a first ISSUE_OPENED
            assert result.classification.intent is Intent.START
            assert result.run_id == "run-001"
            assert result.prev_run_id is None
            assert result.prior_state is State.IDLE

            # Verify DB state directly
            async with sf() as session:
                state, last_seq, current_run_id = await get_state(session, event.resource_key)
            assert state is State.RUNNING
            assert last_seq == 1000
            assert current_run_id == "run-001"

    async def test_second_transition_supersedes(self) -> None:
        """PR_SYNCHRONIZED after PR_OPENED on same resource → SUPERSEDE, prev_run_id set.

        PR_OPENED on an IDLE resource → START.
        PR_SYNCHRONIZED on the same resource (now RUNNING) → SUPERSEDE.
        """
        async with build_inmemory_db() as sf:
            from openbot.domain.events import EventKind, UnifiedEvent

            repo = SqlRunsRepo(session_factory=sf)

            pr_opened = build_pull_request_opened_event(
                pr_number=7,
                event_seq=1000,
                delivery_id="delivery-pr-opened",
            )
            pr_sync = UnifiedEvent(
                channel="github",
                delivery_id="delivery-pr-sync",
                kind=EventKind.PR_SYNCHRONIZED,
                repo=pr_opened.repo,
                actor=pr_opened.actor,
                pr_number=pr_opened.pr_number,
                actor_type="User",
                event_seq=2000,
            )

            # First transition: IDLE → RUNNING (START)
            result_start = await repo.transition(event=pr_opened, new_run_id="run-pr-001")
            assert result_start.classification.intent is Intent.START
            assert result_start.run_id == "run-pr-001"
            assert result_start.prev_run_id is None

            # Second transition: RUNNING + PR_SYNCHRONIZED → SUPERSEDE
            result_supersede = await repo.transition(event=pr_sync, new_run_id="run-pr-002")
            assert result_supersede.classification.intent is Intent.SUPERSEDE
            assert result_supersede.prev_run_id == "run-pr-001"
            assert result_supersede.run_id == "run-pr-002"

            # DB: still RUNNING, current_run_id updated
            async with sf() as session:
                state, seq, current_run = await get_state(session, pr_opened.resource_key)
            assert state is State.RUNNING
            assert current_run == "run-pr-002"
            assert seq == 2000

    async def test_distinct_repos_are_independent(self) -> None:
        """Different repos → independent state machines."""
        async with build_inmemory_db() as sf:
            repo = SqlRunsRepo(session_factory=sf)

            event_repo_a = build_issue_opened_event(
                repo="org/repo-a",
                issue_number=10,
                event_seq=1000,
                delivery_id="del-a",
            )
            event_repo_b = build_issue_opened_event(
                repo="org/repo-b",
                issue_number=10,
                event_seq=2000,
                delivery_id="del-b",
            )

            result_a = await repo.transition(event=event_repo_a, new_run_id="run-a")
            result_b = await repo.transition(event=event_repo_b, new_run_id="run-b")

            # Both are START — neither resource "knows" about the other
            assert result_a.classification.intent is Intent.START
            assert result_b.classification.intent is Intent.START
            assert result_a.run_id == "run-a"
            assert result_b.run_id == "run-b"

            # Verify each resource_key has its own row
            assert event_repo_a.resource_key != event_repo_b.resource_key
            async with sf() as session:
                state_a, _, run_a = await get_state(session, event_repo_a.resource_key)
                state_b, _, run_b = await get_state(session, event_repo_b.resource_key)

            assert state_a is State.RUNNING
            assert state_b is State.RUNNING
            assert run_a == "run-a"
            assert run_b == "run-b"

    async def test_stale_event_is_ignored(self) -> None:
        """An event with a lower event_seq than the persisted high-water mark is dropped."""
        async with build_inmemory_db() as sf:
            repo = SqlRunsRepo(session_factory=sf)
            event_fresh = build_issue_opened_event(
                issue_number=20,
                event_seq=5000,
                delivery_id="del-fresh",
            )
            event_stale = build_issue_opened_event(
                issue_number=20,
                event_seq=1000,  # lower than 5000 → stale
                delivery_id="del-stale",
            )

            # Apply the fresh event first (START)
            result_fresh = await repo.transition(event=event_fresh, new_run_id="run-fresh")
            assert result_fresh.classification.intent is Intent.START

            # Now apply the stale event — should be ignored
            result_stale = await repo.transition(event=event_stale, new_run_id="run-stale")
            assert result_stale.classification.intent is Intent.IGNORE
            assert result_stale.classification.reason == "stale_event"

            # DB state must not have changed
            async with sf() as session:
                state, seq, run_id = await get_state(session, event_fresh.resource_key)
            assert state is State.RUNNING
            assert seq == 5000
            assert run_id == "run-fresh"

    async def test_row_version_advances_on_each_write(self) -> None:
        """row_version increments atomically on each successful transition.

        aiosqlite is a single-writer DB so true concurrent contention cannot
        be exercised in-process.  This test instead verifies that the CAS
        mechanism (``version_id_col``) tracks writes correctly: each
        transition bumps ``row_version`` by one, so any racing real-Postgres
        writer that reads a stale version will receive a StaleDataError and
        retry via the bounded CAS loop.
        """
        async with build_inmemory_db() as sf:
            from openbot.domain.events import EventKind, UnifiedEvent
            from openbot.infrastructure.persistence.models import TaskRun

            repo = SqlRunsRepo(session_factory=sf)

            pr_opened = build_pull_request_opened_event(
                pr_number=42,
                event_seq=1000,
                delivery_id="del-v-start",
            )
            pr_sync = UnifiedEvent(
                channel="github",
                delivery_id="del-v-sync",
                kind=EventKind.PR_SYNCHRONIZED,
                repo=pr_opened.repo,
                actor=pr_opened.actor,
                pr_number=pr_opened.pr_number,
                actor_type="User",
                event_seq=2000,
            )

            await repo.transition(event=pr_opened, new_run_id="run-v-1")
            await repo.transition(event=pr_sync, new_run_id="run-v-2")

            # Inspect the raw row to verify row_version advanced
            async with sf() as session:
                row = await session.get(TaskRun, pr_opened.resource_key)
            assert row is not None
            # Two writes → row_version should be 2 (1 per flush)
            assert row.row_version == 2
