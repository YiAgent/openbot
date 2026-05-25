"""In-memory SQL session factory backed by aiosqlite.

Runs Base.metadata.create_all once per fixture; alembic migrations are
NOT replayed (that's a real_service concern). Use this when you want
the SQLAlchemy code path real but no Postgres dialect features."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from openbot.infrastructure.persistence.models import Base


@asynccontextmanager
async def build_inmemory_db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an async sessionmaker bound to an in-memory aiosqlite DB."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    finally:
        await engine.dispose()


__all__ = ["build_inmemory_db"]
