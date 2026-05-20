# tests/application/dispatcher/test_decide_incremental.py
"""Integration tests: decide_and_enqueue() reads last_reviewed_sha from DB.

Verifies the F4 wiring: when a prior review has stored a head SHA into
task_runs.last_reviewed_sha, decide_and_enqueue reads it and passes it to
compute_diff_scope so DiffScope.is_incremental / is_force_push are set
correctly on the resulting TaskSpec.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from openbot.application.state.runs_repo import store_reviewed_sha, transition
from openbot.dispatcher import decide_and_enqueue
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.persistence import create_schema, make_engine, make_session_factory
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests._fakes.queue import FakeQueue

# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def db_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory SQLite session factory (same pattern as test_runs_repo.py)."""
    engine: AsyncEngine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    yield make_session_factory(engine)
    await engine.dispose()


# ── helpers ───────────────────────────────────────────────────────────────────


_REVIEW_CLASSIFIER = {
    "change_size_class": "s",
    "touches_security_paths": False,
    "is_breaking": False,
    "suggested_subagents": ["correctness"],
}

_RESOURCE_KEY = "github:org/repo:pr:42"


def _sync_event(head_sha: str, before: str) -> UnifiedEvent:
    """PR_SYNCHRONIZED event — the typical incremental-push payload."""
    return UnifiedEvent(
        channel="github",
        delivery_id="del-sync",
        kind=EventKind.PR_SYNCHRONIZED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        issue_number=None,
        pr_number=42,
        installation_id=100,
        comment_body=None,
        raw={
            "before": before,
            "pull_request": {
                "number": 42,
                "additions": 5,
                "deletions": 2,
                "changed_files": 1,
                "head": {"sha": head_sha},
                "base": {"sha": "SHA-base"},
                "body": "Small fix.",
            },
        },
    )


async def _seed_last_reviewed_sha(
    factory: async_sessionmaker[AsyncSession],
    sha: str,
) -> None:
    """Seed the DB with a TaskRun row and a last_reviewed_sha."""
    event = UnifiedEvent(
        channel="github",
        delivery_id="seed-del",
        kind=EventKind.PR_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        pr_number=42,
        event_seq=1_000,
    )
    async with factory() as session:
        await transition(session, event=event, new_run_id="run-seed")
        await session.commit()
    async with factory() as session:
        await store_reviewed_sha(session, _RESOURCE_KEY, sha)
        await session.commit()


def _classifier_response() -> MagicMock:
    response = MagicMock()
    response.choices[0].message.content = json.dumps(_REVIEW_CLASSIFIER)
    return response


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_incremental_detected_when_before_matches_last_reviewed_sha(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """before==last_reviewed_sha → is_incremental=True in the enqueued TaskSpec."""
    from openbot.application.router import dispatch_for

    last_sha = "SHA-reviewed"
    await _seed_last_reviewed_sha(db_factory, last_sha)

    event = _sync_event(head_sha="SHA-new", before=last_sha)
    dispatch = dispatch_for(event)
    assert dispatch is not None

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=_classifier_response()):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event,
            dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue,
            session_factory=db_factory,
            redis=None,
        )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.is_incremental is True
    assert spec.is_force_push is False


@pytest.mark.asyncio
async def test_force_push_detected_when_before_differs_from_last_reviewed_sha(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """before!=last_reviewed_sha → is_force_push=True (history rewritten)."""
    from openbot.application.router import dispatch_for

    await _seed_last_reviewed_sha(db_factory, "SHA-old-reviewed")

    event = _sync_event(head_sha="SHA-new", before="SHA-different-force-push")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=_classifier_response()):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event,
            dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue,
            session_factory=db_factory,
            redis=None,
        )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.is_incremental is False
    assert spec.is_force_push is True


@pytest.mark.asyncio
async def test_first_review_when_no_sha_in_db(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No prior review SHA in DB → last_reviewed_sha=None → first review (not incremental)."""
    from openbot.application.router import dispatch_for

    # Create the row WITHOUT storing a reviewed SHA (simulates first-ever PR open).
    event = _sync_event(head_sha="SHA-first", before="SHA-base")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=_classifier_response()):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event,
            dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue,
            session_factory=db_factory,
            redis=None,
        )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.is_incremental is False
    assert spec.is_force_push is False


@pytest.mark.asyncio
async def test_session_factory_none_falls_back_to_not_incremental() -> None:
    """When session_factory is None (dev mode), incremental is always False."""
    from openbot.application.router import dispatch_for

    event = _sync_event(head_sha="SHA-new", before="SHA-old")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=_classifier_response()):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event,
            dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue,
            session_factory=None,
            redis=None,
        )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.is_incremental is False
    assert spec.is_force_push is False
