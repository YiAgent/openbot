"""DB engine + session lifecycle, using aiosqlite for an in-memory DB.

Production targets Postgres; SQLite covers the cross-dialect SQL we emit.
PG-specific behavior (jsonb operators, etc.) is intentionally avoided in
`models.py` so these tests are sufficient for the schema we ship today.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from openbot.infrastructure.persistence import (
    AuditLog,
    Base,
    CostMeter,
    create_schema,
    make_engine,
    make_session_factory,
    session_scope,
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
async def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(engine)


# ───── create_schema ─────


async def test_create_schema_creates_both_tables(engine: AsyncEngine) -> None:
    # Re-invoking on an already-created schema must be safe.
    await create_schema(engine)
    table_names = set(Base.metadata.tables)
    assert "cost_meter" in table_names
    assert "audit_log" in table_names


# ───── session_scope ─────


async def test_session_scope_commits_on_clean_exit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(factory) as session:
        session.add(AuditLog(phase="started", workflow="triage"))

    # Re-open a fresh session to verify it was persisted.
    async with session_scope(factory) as session:
        from sqlalchemy import select

        result = await session.execute(select(AuditLog))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].phase == "started"


async def test_session_scope_rolls_back_on_exception(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with session_scope(factory) as session:
            session.add(AuditLog(phase="started", workflow="triage"))
            raise Boom()

    async with session_scope(factory) as session:
        from sqlalchemy import select

        result = await session.execute(select(AuditLog))
        assert result.scalars().all() == []


# ───── model defaults ─────


async def test_costmeter_id_and_created_at_auto_populate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from decimal import Decimal

    async with session_scope(factory) as session:
        entry = CostMeter(
            repo="x/y",
            feature="triage",
            task_id="t-1",
            model="anthropic/GLM-5.1",
            prompt_tokens=10,
            completion_tokens=20,
            cost_usd=Decimal("0.01"),
        )
        session.add(entry)
        await session.flush()
        assert entry.id is not None
        assert entry.created_at is not None
