"""SMHarness — shared state-machine / integration test harness dataclass.

Imported by both ``tests/state_machine/conftest.py`` (L2 state-machine tests)
and ``tests/integration/conftest.py`` (L3 integration tests). Extracting the
class here prevents the two conftest files from diverging silently when new
``app.state`` fields are added: any extension to the dataclass is applied once
and immediately visible to both test layers.

The pytest ``sm`` fixture that *populates* this harness lives in each
conftest separately — the fixture bodies are nearly identical but both need to
live in their own conftest directory so pytest scoping rules apply correctly.
"""

from __future__ import annotations

from dataclasses import dataclass

import fakeredis.aioredis
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openbot.infrastructure.persistence.models import State, TaskRun


@dataclass
class SMHarness:
    """One ASGI-level test environment: live ASGI app + FakeRedis + in-memory SQLite.

    ``client``          ``httpx.AsyncClient`` wired to the ASGI app.
    ``redis``           ``FakeRedis`` — same instance the app uses.
    ``session_factory`` bound to the app's engine so assertions can query
                        ``task_runs`` directly without going through the API.
    """

    client: AsyncClient
    redis: fakeredis.aioredis.FakeRedis
    session_factory: async_sessionmaker[AsyncSession]

    async def queue_len(self) -> int:
        """Number of entries currently in the ``openbot.application.workflows`` stream."""
        return await self.redis.xlen("openbot:workflows")

    async def _db_row(self, resource_key: str) -> TaskRun | None:
        async with self.session_factory() as session:
            return await session.get(TaskRun, resource_key)

    async def db_state(self, resource_key: str) -> State:
        """Persisted state for ``resource_key``; ``State.IDLE`` when row absent."""
        row = await self._db_row(resource_key)
        return row.state if row is not None else State.IDLE

    async def db_run_id(self, resource_key: str) -> str | None:
        """``current_run_id`` for ``resource_key``; ``None`` when row absent."""
        row = await self._db_row(resource_key)
        return row.current_run_id if row is not None else None

    async def cancel_flag(self, run_id: str) -> bool:
        """True iff the cancellation Redis key for ``run_id`` is set."""
        return bool(await self.redis.exists(f"openbot:run_cancel:{run_id}"))


__all__ = ["SMHarness"]
