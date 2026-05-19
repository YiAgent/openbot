"""Shared fixtures for middleware tests (slice B+).

`make_ctx` builds a PreflightContext with sensible defaults; tests
override the bits they care about (feature, redis, session_factory,
adapter mock surface, rate_limiter).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from openbot.application.middleware import PreflightContext
from openbot.application.router import Dispatch, derive_task_id
from openbot.application.workflows import maybe_run_chat, maybe_run_triage
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.config_loader import baked_in_defaults
from openbot.infrastructure.llm.model_router import Feature
from openbot.infrastructure.persistence.rate_limiter_redis import RedisRateLimiter

_SENTINEL = object()


def make_event(
    *,
    kind: EventKind = EventKind.ISSUE_OPENED,
    repo: str = "org/r",
    actor: str = "alice",
    actor_type: str | None = "User",
    issue_number: int | None = 7,
    pr_number: int | None = None,
    installation_id: int | None = 100,
    delivery_id: str = "deliv-1",
    comment_body: str | None = None,
    raw: dict | None = None,
) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id=delivery_id,
        kind=kind,
        repo=repo,
        actor=actor,
        actor_type=actor_type,
        issue_number=issue_number,
        pr_number=pr_number,
        installation_id=installation_id,
        comment_body=comment_body,
        raw=raw or {},
    )


def make_ctx(
    event: UnifiedEvent | None = None,
    *,
    feature: Feature = Feature.TRIAGE,
    adapter: Any | None = None,
    redis: Any | None = None,
    session_factory: Any | None = None,
    config: Any = None,
    rate_limiter: Any = _SENTINEL,
) -> PreflightContext:
    """Build a PreflightContext with a default adapter mock.

    `rate_limiter` defaults to a `RedisRateLimiter` wrapping whatever
    `redis` was passed (so existing tests that supply a fakeredis instance
    continue to exercise the full INCR path). Pass an explicit
    `FakeRateLimiter` or `None` to override.
    """
    event = event or make_event()
    handler = maybe_run_chat if feature is Feature.CHAT else maybe_run_triage
    resolved_rate_limiter = RedisRateLimiter(redis) if rate_limiter is _SENTINEL else rate_limiter
    return PreflightContext(
        event=event,
        dispatch=Dispatch(feature, handler, derive_task_id(event)),
        config=config or baked_in_defaults(),
        adapter=adapter or AsyncMock(),
        session_factory=session_factory,
        redis=redis,
        rate_limiter=resolved_rate_limiter,
    )


@pytest.fixture
def event_factory():
    """Test-local factory exposing make_event for fluent overrides."""
    return make_event


@pytest.fixture
def ctx_factory():
    return make_ctx
