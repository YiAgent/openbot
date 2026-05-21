"""State-machine L2 integration harness.

Why we bypass the lifespan
--------------------------
``httpx.ASGITransport`` only sends ``type=http`` scopes — it does NOT send
the ASGI ``type=lifespan`` scope, so ``app.lifespan`` never runs. Instead
of wiring a lifespan manager, we **manually populate ``app.state``** in the
fixture. This is more explicit: each backend (FakeRedis, aiosqlite engine,
GitHubAdapter) is created in the fixture and torn down in its ``finally``
block, giving us precise control without depending on lifespan side-effects.

``httpx.AsyncClient + ASGITransport`` (not ``TestClient``) still runs the
app in the current pytest-asyncio event loop. ``FakeRedis`` is event-loop-
bound; ``TestClient`` creates its own internal loop, causing cross-loop
errors. ASGITransport avoids that.

Backend map (matches what lifespan would write to app.state):
  app.state.redis              FakeRedis
  app.state.cancellation       RedisCancellation(redis_fake)
  app.state.dedup              WebhookDedup(redis_fake)
  app.state.queue              RedisStreamQueue(redis_fake)
  app.state.db_engine          aiosqlite in-memory engine
  app.state.db_session_factory async_sessionmaker bound to engine
  app.state.runs_repo          SqlRunsRepo(session_factory)
  app.state.github_auth        None  (no App creds → no check-run creation)
  app.state.github_adapter     GitHubAdapter(webhook_secret=_SM_SECRET)
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
from tests.state_machine._payloads import _SM_SECRET


@pytest.fixture
async def sm(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SMHarness]:
    """State-machine L2 integration fixture.

    Manually populates ``app.state`` with test doubles instead of running
    the ASGI lifespan (which ``ASGITransport`` doesn't trigger).

    The ``_isolate_openbot_env`` autouse fixture (``tests/conftest.py``)
    strips all ``OPENBOT_*`` vars before each test; we do NOT need them here
    because all backends are created directly rather than read from settings.
    The only setting the app reads inside a request is ``debug_echo_enabled``
    (to choose the workflow handler in ``_resolve_handler``), so we set that.
    """
    from openbot.core.settings import get_settings

    monkeypatch.setenv("OPENBOT_DEBUG_ECHO_ENABLED", "false")
    get_settings.cache_clear()

    redis_fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    session_factory = make_session_factory(engine)

    from openbot.entrypoints.api.app import app

    # Populate app.state with test doubles — mirrors what lifespan would write.
    app.state.redis = redis_fake
    app.state.cancellation = RedisCancellation(redis_fake)
    app.state.resource_lock = RedisResourceLock(redis_fake)
    app.state.dedup = WebhookDedup(redis_fake)
    app.state.queue = RedisStreamQueue(redis_fake)
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory
    app.state.runs_repo = SqlRunsRepo(session_factory)
    app.state.github_auth = None  # no check-run creation in tests
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
