"""CostMeterRepo + AuditLogRepo against in-memory SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from openbot.infrastructure.persistence import (
    AuditLogRepo,
    CostMeterRepo,
    create_schema,
    make_engine,
    make_session_factory,
    rolling_month_window,
    session_scope,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    yield make_session_factory(engine)
    await engine.dispose()


# ───── CostMeterRepo ─────


async def test_record_persists_a_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(factory) as session:
        await CostMeterRepo(session).record(
            repo="YiAgent/openbot",
            feature="triage",
            task_id="deliv-1",
            model="anthropic/GLM-5.1",
            cost_usd=Decimal("0.012345"),
            prompt_tokens=100,
            completion_tokens=50,
        )

    async with session_scope(factory) as session:
        total = await CostMeterRepo(session).sum_for_task("deliv-1")
        # Decimal arithmetic — exact, no float drift.
        assert total == Decimal("0.012345")


async def test_sum_for_task_isolates_by_task_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(factory) as session:
        repo = CostMeterRepo(session)
        for task_id, cost in [
            ("t-A", "0.10"),
            ("t-A", "0.05"),
            ("t-B", "0.99"),
        ]:
            await repo.record(
                repo="x/y",
                feature="review",
                task_id=task_id,
                model="m",
                cost_usd=Decimal(cost),
                prompt_tokens=0,
                completion_tokens=0,
            )

    async with session_scope(factory) as session:
        repo = CostMeterRepo(session)
        assert await repo.sum_for_task("t-A") == Decimal("0.15")
        assert await repo.sum_for_task("t-B") == Decimal("0.99")
        assert await repo.sum_for_task("t-missing") == Decimal("0")


async def test_sum_for_repo_since_filters_by_time(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Insert two records, then query a window that excludes the older one
    # by patching `created_at` directly.
    from openbot.infrastructure.persistence import CostMeter

    long_ago = datetime.now(UTC) - timedelta(days=60)
    recent = datetime.now(UTC) - timedelta(days=1)

    async with session_scope(factory) as session:
        session.add(
            CostMeter(
                repo="x/y",
                feature="fix",
                task_id="old",
                model="m",
                cost_usd=Decimal("10"),
                prompt_tokens=0,
                completion_tokens=0,
                created_at=long_ago,
            )
        )
        session.add(
            CostMeter(
                repo="x/y",
                feature="fix",
                task_id="new",
                model="m",
                cost_usd=Decimal("0.50"),
                prompt_tokens=0,
                completion_tokens=0,
                created_at=recent,
            )
        )

    async with session_scope(factory) as session:
        repo = CostMeterRepo(session)
        window = rolling_month_window(datetime.now(UTC))
        # Only the recent row is inside the 30-day window.
        assert await repo.sum_for_repo_since("x/y", window) == Decimal("0.50")
        # Earlier window catches both.
        assert await repo.sum_for_repo_since(
            "x/y", datetime.now(UTC) - timedelta(days=90)
        ) == Decimal("10.50")


async def test_sum_global_since_includes_all_repos(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(factory) as session:
        repo = CostMeterRepo(session)
        for r in ["a/x", "b/y", "c/z"]:
            await repo.record(
                repo=r,
                feature="chat",
                task_id="t",
                model="m",
                cost_usd=Decimal("0.10"),
                prompt_tokens=0,
                completion_tokens=0,
            )

    async with session_scope(factory) as session:
        total = await CostMeterRepo(session).sum_global_since(datetime.now(UTC) - timedelta(days=1))
        assert total == Decimal("0.30")


# ───── AuditLogRepo ─────


async def test_write_persists_entry_with_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(factory) as session:
        entry = await AuditLogRepo(session).write(
            phase="started",
            delivery_id="d-1",
            repo="x/y",
            actor="yiwang",
            workflow="triage",
            details={"reason": "issue.opened"},
        )
        assert entry.id is not None


async def test_by_delivery_returns_ordered(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(factory) as session:
        repo = AuditLogRepo(session)
        await repo.write(phase="started", delivery_id="d-1", workflow="triage")
        await repo.write(phase="completed", delivery_id="d-1", workflow="triage")
        # Unrelated delivery — must not show up.
        await repo.write(phase="started", delivery_id="d-2", workflow="review")

    async with session_scope(factory) as session:
        rows = await AuditLogRepo(session).by_delivery("d-1")
        phases = [r.phase for r in rows]
        assert phases == ["started", "completed"]


async def test_by_delivery_empty_when_missing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(factory) as session:
        rows = await AuditLogRepo(session).by_delivery("never-existed")
        assert rows == []
