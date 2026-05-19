"""Integration L3 test fixtures.

The integration tests need the same harness as the state-machine L2 tests
(FakeRedis + aiosqlite + ASGI client). Rather than sys.path tricks or
conftest chaining, we duplicate the fixture setup here — the fixture body
is short enough that the duplication cost is lower than the coupling cost.

Why not import from ``tests/state_machine/conftest.py``?
  - pytest adds each conftest's directory to sys.path, so
    ``state_machine.conftest`` is importable at runtime. But Pyright
    (and ruff) treat it as an unresolved import unless the project root
    is also in ``pythonpath``. Duplication avoids the tool noise.
  - The ``SMHarness`` helper is also re-exported so integration test
    files can annotate with it without a second import.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openbot.infrastructure.adapters.github import GitHubAdapter
from openbot.infrastructure.persistence import (
    WebhookDedup,
    create_schema,
    make_engine,
    make_session_factory,
)
from openbot.infrastructure.persistence.cancellation_redis import RedisCancellation
from openbot.infrastructure.persistence.models import State, TaskRun
from openbot.infrastructure.persistence.resource_lock_redis import RedisResourceLock
from openbot.infrastructure.persistence.runs_repo_impl import SqlRunsRepo
from openbot.infrastructure.queue.enqueue import RedisStreamQueue

# Use the same secret and payloads as the L2 state-machine tests so
# assertions on delivery IDs and resource keys are compatible.
from tests.state_machine._payloads import _SM_SECRET


@dataclass
class SMHarness:
    """One integration test environment: live ASGI app + FakeRedis + aiosqlite.

    Same interface as ``tests/state_machine/conftest.SMHarness`` — they can
    be used interchangeably in fixture type annotations.
    """

    client: AsyncClient
    redis: fakeredis.aioredis.FakeRedis
    session_factory: async_sessionmaker[AsyncSession]

    async def queue_len(self) -> int:
        """Entries currently in the ``openbot.application.workflows`` stream."""
        return await self.redis.xlen("openbot:workflows")

    async def _db_row(self, resource_key: str) -> TaskRun | None:
        async with self.session_factory() as session:
            return await session.get(TaskRun, resource_key)

    async def db_state(self, resource_key: str) -> State:
        """Persisted state for ``resource_key``; ``State.IDLE`` when absent."""
        row = await self._db_row(resource_key)
        return row.state if row is not None else State.IDLE

    async def db_run_id(self, resource_key: str) -> str | None:
        """``current_run_id`` for ``resource_key``; ``None`` when absent."""
        row = await self._db_row(resource_key)
        return row.current_run_id if row is not None else None

    async def cancel_flag(self, run_id: str) -> bool:
        """True iff the Redis cancellation key for ``run_id`` is set."""
        return bool(await self.redis.exists(f"openbot:run_cancel:{run_id}"))


@pytest.fixture
async def sm(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SMHarness]:
    """Integration L3 harness — identical to the L2 ``sm`` fixture.

    The fixture name is intentionally the same so integration test files
    can use ``sm: SMHarness`` without caring which conftest provides it.
    """
    from openbot.core.settings import get_settings

    monkeypatch.setenv("OPENBOT_DEBUG_ECHO_ENABLED", "false")
    get_settings.cache_clear()

    redis_fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    session_factory = make_session_factory(engine)

    from openbot.entrypoints.api.app import app

    app.state.redis = redis_fake
    app.state.cancellation = RedisCancellation(redis_fake)
    app.state.resource_lock = RedisResourceLock(redis_fake)
    app.state.dedup = WebhookDedup(redis_fake)
    app.state.queue = RedisStreamQueue(redis_fake)
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory
    app.state.runs_repo = SqlRunsRepo(session_factory)
    app.state.github_auth = None
    app.state.github_adapter = GitHubAdapter(webhook_secret=_SM_SECRET, auth=None)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield SMHarness(
                client=client,
                redis=redis_fake,
                session_factory=session_factory,
            )
    finally:
        await redis_fake.aclose()
        await engine.dispose()
        get_settings.cache_clear()
