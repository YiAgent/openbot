"""LangGraph agent checkpointer — Postgres-backed, async.

One checkpointer is created per Worker process lifetime and shared
across all concurrent consumers. ``agent_checkpointer(dsn)`` is an
async context manager: the caller boots it before ``consume_loop``
and shuts it down on exit.

When ``dsn`` is ``None`` (local dev without Postgres) the context
manager yields ``None`` — handlers receiving ``None`` skip
checkpointing entirely (same graceful-degrade pattern as
``ctx.sandbox_factory is None``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


# Indirection used by tests to monkeypatch the factory without touching
# the real asyncpg connection machinery.
@asynccontextmanager
async def _AsyncPostgresSaver_from_conn_string(
    dsn: str,
) -> AsyncGenerator[AsyncPostgresSaver, None]:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver as _Real

    async with _Real.from_conn_string(dsn) as saver:
        yield saver


@asynccontextmanager
async def agent_checkpointer(
    dsn: str | None,
) -> AsyncGenerator[AsyncPostgresSaver | None, None]:
    """Yield a ready-to-use ``AsyncPostgresSaver``, or ``None`` if no DSN.

    ``setup()`` is called once — it is idempotent and creates the four
    LangGraph checkpoint tables if they don't already exist.

    Usage::

        async with agent_checkpointer(settings.postgres_url) as cp:
            await consume_loop(redis, ..., agent_checkpointer=cp)
    """
    if dsn is None:
        yield None
        return

    async with _AsyncPostgresSaver_from_conn_string(dsn) as saver:
        await saver.setup()
        yield saver
