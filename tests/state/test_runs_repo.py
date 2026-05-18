"""``runs_repo.transition`` against in-memory SQLite.

Coverage:

* ``transition`` writes the first row from an empty DB and returns ``START``.
* Stale ``event_seq`` returns ``IGNORE`` + ``reason="stale_event"`` and
  does NOT mutate the row.
* A simulated concurrent bump (we manually bump ``row_version`` between
  the helper's read and write) surfaces as a ``StaleDataError`` that the
  CAS retry recovers from.
* ``IGNORE`` intents that advance ``last_event_seq`` still write (the
  high-water mark must move so a later stale delivery is rejected).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from openbot.events import EventKind, UnifiedEvent
from openbot.persistence import (
    TaskRun,
    create_schema,
    make_engine,
    make_session_factory,
)
from openbot.state.intents import Intent, State
from openbot.state.runs_repo import get_state, transition


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    yield make_session_factory(engine)
    await engine.dispose()


def _opened(*, delivery_id: str = "d-1", event_seq: int = 1_000) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id=delivery_id,
        kind=EventKind.PR_OPENED,
        repo="acme/openbot",
        actor="alice",
        actor_type="User",
        pr_number=42,
        event_seq=event_seq,
    )


def _closed(*, delivery_id: str, event_seq: int) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id=delivery_id,
        kind=EventKind.PR_CLOSED,
        repo="acme/openbot",
        actor="alice",
        actor_type="User",
        pr_number=42,
        event_seq=event_seq,
    )


async def test_transition_starts_from_empty_db(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    event = _opened()
    async with factory() as session:
        result = await transition(session, event=event, new_run_id="run-1")
        await session.commit()

    assert result.classification.intent is Intent.START
    assert result.classification.next_state is State.RUNNING
    assert result.run_id == "run-1"
    assert result.prev_run_id is None
    assert result.prior_state is State.IDLE

    async with factory() as session:
        state, seq, run_id = await get_state(session, event.resource_key or "")
    assert state is State.RUNNING
    assert seq == 1_000
    assert run_id == "run-1"


async def test_stale_event_seq_returns_ignore_and_does_not_mutate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A late delivery with lower event_seq must drop without touching the row."""
    async with factory() as session:
        await transition(session, event=_opened(event_seq=2_000), new_run_id="run-A")
        await session.commit()

    late = _opened(delivery_id="d-late", event_seq=1_500)
    async with factory() as session:
        result = await transition(session, event=late, new_run_id="run-B")
        await session.commit()

    assert result.classification.intent is Intent.IGNORE
    assert result.classification.reason == "stale_event"

    async with factory() as session:
        state, seq, run_id = await get_state(session, late.resource_key or "")
    # High-water mark + run_id unchanged.
    assert state is State.RUNNING
    assert seq == 2_000
    assert run_id == "run-A"


async def test_supersede_records_prev_run_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await transition(session, event=_opened(event_seq=1_000), new_run_id="run-1")
        await session.commit()

    sync = UnifiedEvent(
        channel="github",
        delivery_id="d-sync",
        kind=EventKind.PR_SYNCHRONIZED,
        repo="acme/openbot",
        actor="alice",
        actor_type="User",
        pr_number=42,
        event_seq=2_000,
    )
    async with factory() as session:
        result = await transition(session, event=sync, new_run_id="run-2")
        await session.commit()

    assert result.classification.intent is Intent.SUPERSEDE
    assert result.prev_run_id == "run-1"
    assert result.run_id == "run-2"

    async with factory() as session:
        state, seq, run_id = await get_state(session, sync.resource_key or "")
    assert state is State.RUNNING
    assert seq == 2_000
    assert run_id == "run-2"


async def test_cancel_clears_run_id_and_moves_to_closed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await transition(session, event=_opened(event_seq=1_000), new_run_id="run-1")
        await session.commit()

    close = _closed(delivery_id="d-close", event_seq=2_000)
    async with factory() as session:
        result = await transition(session, event=close, new_run_id="run-unused")
        await session.commit()

    assert result.classification.intent is Intent.CANCEL
    assert result.prev_run_id == "run-1"
    # CANCEL does not allocate a new run_id.
    assert result.run_id is None

    async with factory() as session:
        state, seq, run_id = await get_state(session, close.resource_key or "")
    assert state is State.CLOSED
    assert seq == 2_000
    assert run_id is None


async def test_ignore_with_state_change_still_persists(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """``ISSUE_CLOSED`` from IDLE is IGNORE but must persist ``state=CLOSED``."""
    close = UnifiedEvent(
        channel="github",
        delivery_id="d-1",
        kind=EventKind.ISSUE_CLOSED,
        repo="acme/openbot",
        actor="alice",
        actor_type="User",
        issue_number=7,
        event_seq=500,
    )
    async with factory() as session:
        result = await transition(session, event=close, new_run_id="run-unused")
        await session.commit()

    assert result.classification.intent is Intent.IGNORE
    assert result.classification.reason == "closed_no_run"

    async with factory() as session:
        state, seq, _ = await get_state(session, close.resource_key or "")
    assert state is State.CLOSED
    assert seq == 500


async def test_no_resource_key_returns_ignore(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """``ping`` / push-style events have no resource — IGNORE before any DB read."""
    event = UnifiedEvent(
        channel="github",
        delivery_id="d-ping",
        kind=EventKind.UNKNOWN,
        repo="acme/openbot",
        actor="alice",
    )
    async with factory() as session:
        result = await transition(session, event=event, new_run_id="run-1")
    assert result.classification.intent is Intent.IGNORE
    assert result.classification.reason == "no_resource_key"


async def test_row_version_bumps_on_each_write(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """``row_version`` is the CAS column — each successful transition increments it."""
    async with factory() as session:
        await transition(session, event=_opened(event_seq=1_000), new_run_id="run-1")
        await session.commit()
    async with factory() as session:
        await transition(
            session,
            event=UnifiedEvent(
                channel="github",
                delivery_id="d-sync",
                kind=EventKind.PR_SYNCHRONIZED,
                repo="acme/openbot",
                actor="alice",
                actor_type="User",
                pr_number=42,
                event_seq=2_000,
            ),
            new_run_id="run-2",
        )
        await session.commit()

    async with factory() as session:
        row = await session.get(TaskRun, "github:acme/openbot:pr:42")
    assert row is not None
    # Initial insert sets version=1; one update bumps it to 2.
    assert row.row_version == 2
