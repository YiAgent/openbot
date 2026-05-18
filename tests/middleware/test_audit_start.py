"""AuditStartMiddleware — STARTED row written at end of preflight.

Spec anchor: harness-spec §3 M3 (chain position end) + §3 M9
(coordinate with audit_lifecycle via ``ctx.cache``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from openbot.application.middleware import MiddlewareResult
from openbot.application.middleware.audit import AUDIT_STARTED_CACHE_KEY, AuditStartMiddleware
from openbot.infrastructure.persistence.models import Base, Workflow, WorkflowPhase
from openbot.infrastructure.persistence.repository import AuditLogRepo
from tests.middleware.conftest import make_ctx


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory aiosqlite — same shape production uses for Postgres.

    Must yield + dispose: a plain ``return`` keeps the engine alive past
    test teardown, and aiosqlite's ``Connection.__del__`` then fires a
    ``ResourceWarning`` against whichever later test happens to be
    running at GC time (matches ``test_budget.py`` pattern).
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


async def test_writes_started_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Happy path — middleware writes one STARTED row, sets cache flag."""
    ctx = make_ctx(session_factory=session_factory)
    decision = await AuditStartMiddleware()(ctx)

    assert decision.result is MiddlewareResult.PROCEED
    assert ctx.cache[AUDIT_STARTED_CACHE_KEY] is True

    async with session_factory() as session:
        rows = await AuditLogRepo(session).by_delivery(ctx.event.delivery_id)
    assert len(rows) == 1
    assert rows[0].phase is WorkflowPhase.STARTED
    assert rows[0].workflow is Workflow.TRIAGE


async def test_no_session_factory_returns_proceed_silently() -> None:
    """Dev mode (no Postgres) — middleware no-ops, no flag set."""
    ctx = make_ctx(session_factory=None)
    decision = await AuditStartMiddleware()(ctx)
    assert decision.result is MiddlewareResult.PROCEED
    # Crucially, no cache flag → lifecycle helper still writes its
    # own STARTED in the fallback path.
    assert AUDIT_STARTED_CACHE_KEY not in ctx.cache


async def test_db_failure_falls_open(caplog: pytest.LogCaptureFixture) -> None:
    """A raised DB error → PROCEED + WARNING log, no cache flag.

    Lifecycle helper then writes STARTED itself (legacy path), so the
    audit row appears either way; this middleware just shifts where
    the write happens.
    """
    # AsyncMock that raises on __aenter__ — simulates DB connection
    # exhaustion / network partition.
    bad_factory = MagicMock()
    bad_factory.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("db down"))

    ctx = make_ctx(session_factory=bad_factory)
    with caplog.at_level(logging.ERROR, logger="openbot.application.middleware.audit"):
        decision = await AuditStartMiddleware()(ctx)

    assert decision.result is MiddlewareResult.PROCEED
    assert AUDIT_STARTED_CACHE_KEY not in ctx.cache
    assert any(r.message == "audit_start_write_failed" for r in caplog.records)
