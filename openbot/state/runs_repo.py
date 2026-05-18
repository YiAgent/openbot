"""``task_runs`` CRUD — bounded CAS retry + stale-seq rejection.

This is the persistence layer the receive side calls between
``classifier.classify`` and ``enqueue``. The transition helper is the
only writer of ``task_runs`` rows; that's enforced by convention rather
than the DB.

CAS strategy: ``TaskRun.row_version`` is the SQLAlchemy
``version_id_col``. Combined with the per-resource Redis lock in
``state.resource_lock`` the contention window is essentially zero — but
we keep a tight retry on ``StaleDataError`` so a lock TTL expiry doesn't
turn into a silent double-write.

Stale-seq rejection: when the candidate event's ``event_seq`` is lower
than the persisted ``last_event_seq``, ``transition`` returns
``IGNORE`` + ``reason='stale_event'`` and leaves the row untouched. This
handles GitHub's "delivery N+1 arrives before delivery N" path without
a Lamport clock.

Pure UPDATE / SELECT is intentionally cross-dialect: no ON CONFLICT, no
RETURNING, so the same code runs against aiosqlite in tests and asyncpg
in production.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from openbot.persistence.models import TaskRun
from openbot.state.classifier import classify
from openbot.state.intents import EventClassification, Intent, State

if TYPE_CHECKING:
    from openbot.events import UnifiedEvent

_logger = logging.getLogger(__name__)

# Bounded retry on a CAS miss. Three attempts is far beyond the realistic
# contention window — with the per-resource Redis lock held, contention
# can only come from the lock falling open (TTL expiry + slow handler).
_MAX_CAS_ATTEMPTS: Final = 3


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """What ``transition`` returns to the receive-side orchestrator.

    ``classification``  full ``EventClassification`` — intent, next state, reason.
    ``run_id``          the run identifier the receive side should use.
                        For START/SUPERSEDE this is a fresh id allocated by
                        the caller; the helper stores it as ``current_run_id``
                        on the new row. For CANCEL/IGNORE this is the value
                        that was active (may be None when no prior run).
    ``prev_run_id``     the run id that was active **before** this transition.
                        Set when intent is SUPERSEDE or CANCEL so the worker
                        knows whom to cancel; None otherwise.
    ``prior_state``     the state value the row had before we wrote.
                        Useful for the debug echo handler's trace line.
    """

    classification: EventClassification
    run_id: str | None
    prev_run_id: str | None
    prior_state: State


async def get_state(session: AsyncSession, resource_key: str) -> tuple[State, int, str | None]:
    """Read the persisted ``(state, last_event_seq, current_run_id)``.

    Missing row → ``(State.IDLE, 0, None)``. ``transition`` calls this
    first to compute the classification; tests call it to assert state
    after a scenario completes.
    """
    row = await session.get(TaskRun, resource_key)
    if row is None:
        return State.IDLE, 0, None
    return row.state, row.last_event_seq, row.current_run_id


async def transition(
    session: AsyncSession,
    *,
    event: UnifiedEvent,
    new_run_id: str,
) -> TransitionResult:
    """Classify ``event`` against the persisted state and (maybe) write a new row.

    Returns a ``TransitionResult`` describing what happened. The caller
    is responsible for ``session.commit()`` on success and for using
    ``classification.intent`` to drive the enqueue / no-op decision.

    Stale-seq rejection: when ``event.event_seq`` is strictly less than
    the persisted ``last_event_seq`` the helper returns
    ``Intent.IGNORE`` + ``reason="stale_event"`` and does NOT write.

    CAS retry: a ``StaleDataError`` from the ORM (another transaction
    bumped ``row_version`` between our read and our write) triggers a
    re-read + re-classify + re-write up to ``_MAX_CAS_ATTEMPTS``. After
    that the helper gives up and returns ``Intent.IGNORE`` +
    ``reason="cas_failed"`` — the receive side falls open and the next
    event for this resource will recover the state.
    """
    resource_key = event.resource_key
    if resource_key is None:
        # No resource — caller should treat as IGNORE. We still classify
        # so the reason is informative.
        return TransitionResult(
            classification=EventClassification(
                intent=Intent.IGNORE, next_state=State.IDLE, reason="no_resource_key"
            ),
            run_id=None,
            prev_run_id=None,
            prior_state=State.IDLE,
        )

    for attempt in range(1, _MAX_CAS_ATTEMPTS + 1):
        row = await session.get(TaskRun, resource_key)
        prior_state = row.state if row else State.IDLE
        prior_seq = row.last_event_seq if row else 0
        prior_run_id = row.current_run_id if row else None

        # Stale event — drop before the classifier so the audit log
        # reason is informative ("stale_event" vs whatever the matrix
        # would have decided).
        if event.event_seq and event.event_seq < prior_seq:
            return TransitionResult(
                classification=EventClassification(
                    intent=Intent.IGNORE, next_state=prior_state, reason="stale_event"
                ),
                run_id=prior_run_id,
                prev_run_id=None,
                prior_state=prior_state,
            )

        classification = classify(event, prior_state)

        if classification.intent is Intent.IGNORE:
            # Even an IGNORE may need to advance ``last_event_seq`` /
            # ``state`` (e.g. ``ISSUE_CLOSED`` while IDLE → state=CLOSED).
            # Only persist when the classifier says the next state
            # differs from the prior state OR when ``event.event_seq``
            # advances the high-water mark — otherwise we'd thrash the
            # row for every bot-author / no-mention event.
            wants_write = classification.next_state is not prior_state or (
                event.event_seq and event.event_seq > prior_seq
            )
            if not wants_write:
                return TransitionResult(
                    classification=classification,
                    run_id=prior_run_id,
                    prev_run_id=None,
                    prior_state=prior_state,
                )
            try:
                await _write_row(
                    session,
                    row=row,
                    resource_key=resource_key,
                    state=classification.next_state,
                    current_run_id=None
                    if classification.next_state in (State.CLOSED, State.IDLE, State.CANCELLED)
                    else prior_run_id,
                    last_event_seq=max(prior_seq, event.event_seq or prior_seq),
                    last_intent=classification.intent,
                    last_delivery_id=event.delivery_id,
                )
            except StaleDataError:
                await session.rollback()
                if attempt == _MAX_CAS_ATTEMPTS:
                    return _cas_failed(prior_state, prior_run_id)
                continue
            return TransitionResult(
                classification=classification,
                run_id=prior_run_id,
                prev_run_id=None,
                prior_state=prior_state,
            )

        # START / SUPERSEDE / CANCEL: bump the row.
        prev_run_id_for_signal = (
            prior_run_id if classification.intent in (Intent.SUPERSEDE, Intent.CANCEL) else None
        )
        run_id_for_row = new_run_id if classification.intent is not Intent.CANCEL else None
        try:
            await _write_row(
                session,
                row=row,
                resource_key=resource_key,
                state=classification.next_state,
                current_run_id=run_id_for_row,
                last_event_seq=max(prior_seq, event.event_seq or prior_seq),
                last_intent=classification.intent,
                last_delivery_id=event.delivery_id,
            )
        except StaleDataError:
            await session.rollback()
            if attempt == _MAX_CAS_ATTEMPTS:
                _logger.warning(
                    "task_runs_cas_failed",
                    extra={"resource_key": resource_key, "delivery_id": event.delivery_id},
                )
                return _cas_failed(prior_state, prior_run_id)
            continue

        return TransitionResult(
            classification=classification,
            run_id=run_id_for_row,
            prev_run_id=prev_run_id_for_signal,
            prior_state=prior_state,
        )

    # Loop exhausted — defensive; we should have hit one of the returns above.
    return _cas_failed(State.IDLE, None)


async def _write_row(
    session: AsyncSession,
    *,
    row: TaskRun | None,
    resource_key: str,
    state: State,
    current_run_id: str | None,
    last_event_seq: int,
    last_intent: Intent,
    last_delivery_id: str | None,
) -> None:
    """Insert or version-bump the row. The bump itself triggers the CAS
    check via ``version_id_col``.

    The helper does NOT commit — callers control the transaction so
    that the enqueue + write happen under one boundary.
    """
    if row is None:
        new_row = TaskRun(
            resource_key=resource_key,
            state=state,
            current_run_id=current_run_id,
            last_event_seq=last_event_seq,
            last_intent=last_intent,
            last_delivery_id=last_delivery_id,
            row_version=1,
        )
        session.add(new_row)
        await session.flush()
        return
    row.state = state
    row.current_run_id = current_run_id
    row.last_event_seq = last_event_seq
    row.last_intent = last_intent
    row.last_delivery_id = last_delivery_id
    row.row_version = row.row_version + 1
    await session.flush()


def _cas_failed(prior_state: State, prior_run_id: str | None) -> TransitionResult:
    return TransitionResult(
        classification=EventClassification(
            intent=Intent.IGNORE, next_state=prior_state, reason="cas_failed"
        ),
        run_id=prior_run_id,
        prev_run_id=None,
        prior_state=prior_state,
    )


__all__ = [
    "TransitionResult",
    "get_state",
    "transition",
]
