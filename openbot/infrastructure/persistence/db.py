"""Async SQLAlchemy engine + session lifecycle.

Production targets PostgreSQL via asyncpg
  postgresql+asyncpg://user:pass@host:5432/db

Unit tests use aiosqlite for an in-memory database
  sqlite+aiosqlite:///:memory:

Both share the same model definitions and SQL (we deliberately avoid
JSONB / PG-only types in `models.py`).

Engines are constructed once at startup via `make_engine()` and held on
`app.state`. A short-lived `AsyncSession` is created per workflow run via
the `sessionmaker` and disposed inside an async context manager.

Note: startup runs `create_schema()` (a `Base.metadata.create_all`) — sync
DDL inside the async lifespan. The same helper also powers the explicit
`make db-init` bootstrap command so local dev and first-run deploys share
one schema-initialization path.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_POOL_SIZE: Final = 5  # small; OpenBot is a single-tenant maintainer bot
_POOL_MAX_OVERFLOW: Final = 5
_POOL_RECYCLE_SECONDS: Final = 30 * 60  # avoid stale connections behind PG idle timeout


def make_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an async engine for either Postgres (asyncpg) or SQLite (aiosqlite).

    SQLite uses StaticPool which rejects `pool_size` / `max_overflow`, so we
    only pass pool tuning for non-SQLite dialects.
    """
    is_sqlite = url.startswith("sqlite")
    kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True}
    if not is_sqlite:
        kwargs["pool_size"] = _POOL_SIZE
        kwargs["max_overflow"] = _POOL_MAX_OVERFLOW
        kwargs["pool_recycle"] = _POOL_RECYCLE_SECONDS
    return create_async_engine(url, **kwargs)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessionmaker for `async with sessionmaker() as s: ...`."""
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,  # let callers read attributes after commit
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Yield a session that commits on clean exit and rolls back on error.

    Use:
        async with session_scope(factory) as session:
            session.add(record)
            # auto-commits on `__aexit__` if no exception raised
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_schema(engine: AsyncEngine) -> None:
    """First-run schema creation via `Base.metadata.create_all`.

    Sufficient for the current schema bootstrap path — the app/worker startup
    and `make db-init` both call the same helper so the initial schema story
    stays explicit and consistent.
    """
    from openbot.infrastructure.persistence.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
