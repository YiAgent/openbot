"""Chat-feature rate-limit middleware — per-user/day + per-repo/hour."""

from __future__ import annotations

from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from openbot.application.middleware import MiddlewareResult
from openbot.application.middleware.rate_limit import RateLimitMiddleware
from openbot.domain.events import EventKind
from openbot.infrastructure.llm.model_router import Feature
from tests.application.middleware.conftest import make_ctx, make_event


def _chat_event(*, comment_body: str = "@openbot help", actor: str = "alice") -> object:
    return make_event(
        kind=EventKind.ISSUE_COMMENT_CREATED,
        actor=actor,
        comment_body=comment_body,
    )


def _adapter(role: str = "none") -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_actor_role = AsyncMock(return_value=role)
    return adapter


# ───── feature gating ─────


async def test_skips_non_chat_features() -> None:
    """Triage / review / fix are governed by budget, not chat rate limits."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    decision = await RateLimitMiddleware()(
        make_ctx(feature=Feature.TRIAGE, redis=redis, adapter=_adapter())
    )
    assert decision.result is MiddlewareResult.PROCEED


async def test_fall_open_when_redis_missing() -> None:
    decision = await RateLimitMiddleware()(
        make_ctx(
            event=_chat_event(),
            feature=Feature.CHAT,
            redis=None,
            adapter=_adapter(),
        )
    )
    assert decision.result is MiddlewareResult.PROCEED


# ───── exempt roles ─────


@pytest.mark.parametrize("role", ["owner", "collaborator"])
async def test_exempt_roles_skip_counters(role: str) -> None:
    """A maintainer flooding chat shouldn't be rate-limited."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    adapter = _adapter(role=role)
    for _ in range(100):  # default cap is 20/day; well past
        decision = await RateLimitMiddleware()(
            make_ctx(
                event=_chat_event(),
                feature=Feature.CHAT,
                redis=redis,
                adapter=adapter,
            )
        )
        assert decision.result is MiddlewareResult.PROCEED


# ───── per-user-per-day ─────


async def test_per_user_per_day_blocks_at_cap() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    adapter = _adapter(role="read")
    last_decision = None
    for _ in range(21):  # default cap = 20
        last_decision = await RateLimitMiddleware()(
            make_ctx(
                event=_chat_event(),
                feature=Feature.CHAT,
                redis=redis,
                adapter=adapter,
            )
        )
    assert last_decision is not None
    assert last_decision.result is MiddlewareResult.BLOCKED
    assert last_decision.reason == "rate_limit_per_user_per_day"
    assert last_decision.announce_key is not None
    assert last_decision.announce_ttl == 86400


async def test_per_user_counter_caches_role_lookup() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    adapter = _adapter(role="read")
    ctx = make_ctx(
        event=_chat_event(),
        feature=Feature.CHAT,
        redis=redis,
        adapter=adapter,
    )
    await RateLimitMiddleware()(ctx)
    await RateLimitMiddleware()(ctx)  # same ctx → cache hit
    # Even with the same ctx, two passes through the middleware should
    # only call get_actor_role once.
    assert adapter.get_actor_role.await_count == 1


# ───── injection defense ─────


async def test_malicious_actor_login_skips_user_counter() -> None:
    """An actor login outside GitHub's grammar must NOT compose a Redis key."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    adapter = _adapter(role="read")
    bad_event = _chat_event(actor="evil:user\nrl:admin:override")
    decision = await RateLimitMiddleware()(
        make_ctx(event=bad_event, feature=Feature.CHAT, redis=redis, adapter=adapter)
    )
    # No Redis key should have been written for the user counter.
    keys = await redis.keys("rl:user:*")
    assert keys == []
    # Repo counter still runs.
    assert decision.result is MiddlewareResult.PROCEED


async def test_malicious_repo_skips_repo_counter() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    adapter = _adapter(role="read")
    bad_event = make_event(
        kind=EventKind.ISSUE_COMMENT_CREATED,
        repo="not/a:real/repo\nrl:repo:override",
        comment_body="@openbot help",
    )
    decision = await RateLimitMiddleware()(
        make_ctx(event=bad_event, feature=Feature.CHAT, redis=redis, adapter=adapter)
    )
    keys = await redis.keys("rl:repo:*")
    assert keys == []
    assert decision.result is MiddlewareResult.PROCEED


# ───── redis transient ─────


async def test_falls_open_on_redis_pipeline_error() -> None:
    """Redis flap during INCR must NOT block all chat indefinitely."""
    failing_redis = AsyncMock()
    failing_redis.pipeline = lambda transaction=False: _RaisingPipeline()
    decision = await RateLimitMiddleware()(
        make_ctx(
            event=_chat_event(),
            feature=Feature.CHAT,
            redis=failing_redis,
            adapter=_adapter(role="read"),
        )
    )
    assert decision.result is MiddlewareResult.PROCEED


class _RaisingPipeline:
    def incr(self, *_args, **_kwargs):
        return self

    def expire(self, *_args, **_kwargs):
        return self

    async def execute(self):
        raise RuntimeError("redis down")
