"""Integration L3 test fixtures.

Shares ``SMHarness`` with the L2 state-machine tests via
``tests._fakes.sm_harness`` — a common module that keeps the dataclass in one
place so adding a new ``app.state`` field doesn't require updating two files.
The ``sm`` fixture body is kept local here because it needs its own conftest
scope and has minor differences in comment context from the L2 version.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from openbot.infrastructure.adapters.github import GitHubAdapter
from openbot.infrastructure.persistence import (
    WebhookDedup,
    create_schema,
    make_engine,
    make_session_factory,
)
from openbot.infrastructure.persistence.cancellation_redis import RedisCancellation
from openbot.infrastructure.persistence.resource_lock_redis import RedisResourceLock
from openbot.infrastructure.persistence.runs_repo_impl import SqlRunsRepo
from openbot.infrastructure.queue.enqueue import RedisStreamQueue
from tests._fakes.sm_harness import SMHarness

# Use the same secret and payloads as the L2 state-machine tests so
# assertions on delivery IDs and resource keys are compatible.
from tests.state_machine._payloads import _SM_SECRET


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
