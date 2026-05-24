"""Pre-flight runner + announce_once helper — harness spec §3 M3 / §9.2.

Slice A only exercises the FRAMEWORK; real gates land in slice B/C.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from openbot.application.middleware import (
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
    announce_once,
    run_preflight,
)
from openbot.application.router import Dispatch, derive_task_id
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.application.use_cases import maybe_run_triage
from openbot.dispatcher.classifier import TriageClassifierOutput
from openbot.domain.checkout import CheckoutSpec, CloneStrategy
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from openbot.infrastructure.config_loader import baked_in_defaults
from openbot.infrastructure.persistence.models import WorkflowPhase


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="deliv-1",
        kind=EventKind.ISSUE_OPENED,
        repo="org/r",
        actor="u",
        actor_type="User",
        issue_number=1,
        installation_id=99,
    )


def _ctx(
    *,
    adapter: Any | None = None,
    redis: Any | None = None,
) -> PreflightContext:
    event = _event()
    return PreflightContext(
        event=event,
        dispatch=Dispatch(Feature.TRIAGE, maybe_run_triage, derive_task_id(event)),
        config=baked_in_defaults(),
        adapter=adapter or AsyncMock(),
        session_factory=None,  # slice A: audit-write is a smoke check elsewhere
        redis=redis,
    )


@dataclass
class _NamedMiddleware:
    """Test helper — wraps an async callable with a `.name` attribute."""

    name: str
    decision: MiddlewareDecision
    calls: list[str]

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        self.calls.append(self.name)
        return self.decision


# ───── ordering / short-circuit ─────


async def test_empty_middleware_chain_returns_proceed() -> None:
    decision = await run_preflight(_ctx(), [])
    assert decision.result is MiddlewareResult.PROCEED


async def test_runs_in_order_until_first_block() -> None:
    calls: list[str] = []
    mws = [
        _NamedMiddleware("a", MiddlewareDecision.proceed(), calls),
        _NamedMiddleware(
            "b",
            MiddlewareDecision(result=MiddlewareResult.BLOCKED, reason="b_blocked"),
            calls,
        ),
        # `c` must not run — short-circuited by `b`.
        _NamedMiddleware("c", MiddlewareDecision.proceed(), calls),
    ]
    decision = await run_preflight(_ctx(), mws)
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "b_blocked"
    assert calls == ["a", "b"]


async def test_proceeds_when_no_middleware_blocks() -> None:
    calls: list[str] = []
    mws = [
        _NamedMiddleware("a", MiddlewareDecision.proceed(), calls),
        _NamedMiddleware("b", MiddlewareDecision.proceed(), calls),
    ]
    decision = await run_preflight(_ctx(), mws)
    assert decision.result is MiddlewareResult.PROCEED
    assert calls == ["a", "b"]


async def test_raising_middleware_does_not_break_chain() -> None:
    """A buggy gate must not become a 5xx that GitHub retries forever."""
    calls: list[str] = []

    class _Boom:
        name = "boom"

        async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
            raise RuntimeError("explode")

    mws = [
        _Boom(),
        _NamedMiddleware("after", MiddlewareDecision.proceed(), calls),
    ]
    decision = await run_preflight(_ctx(), mws)
    assert decision.result is MiddlewareResult.PROCEED
    # The "after" middleware still got its turn.
    assert calls == ["after"]


# ───── BLOCKED side effects: comment + audit ─────


async def test_blocked_with_comment_posts_via_adapter() -> None:
    adapter = AsyncMock()
    adapter.reply = AsyncMock(return_value={"id": 1})
    ctx = _ctx(adapter=adapter)
    mws = [
        _NamedMiddleware(
            "x",
            MiddlewareDecision(
                result=MiddlewareResult.BLOCKED,
                reason="x_blocked",
                comment="hello",
                audit_phase=WorkflowPhase.REJECTED,
            ),
            [],
        )
    ]
    await run_preflight(ctx, mws)
    adapter.reply.assert_awaited_once()
    posted_event, posted_msg = adapter.reply.await_args.args
    assert posted_msg == "hello"
    assert posted_event is ctx.event


async def test_blocked_without_comment_does_not_reply() -> None:
    adapter = AsyncMock()
    adapter.reply = AsyncMock()
    ctx = _ctx(adapter=adapter)
    mws = [
        _NamedMiddleware(
            "x",
            MiddlewareDecision(result=MiddlewareResult.BLOCKED, reason="silent"),
            [],
        )
    ]
    await run_preflight(ctx, mws)
    adapter.reply.assert_not_awaited()


# ───── announce_once (§9.2) ─────


async def test_announce_once_posts_first_time() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    reply = AsyncMock(return_value=None)
    ok = await announce_once(redis, key="k", ttl_seconds=60, reply=reply)
    assert ok is True
    reply.assert_awaited_once()


async def test_announce_once_idempotent_within_ttl() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    reply = AsyncMock(return_value=None)
    first = await announce_once(redis, key="k2", ttl_seconds=60, reply=reply)
    second = await announce_once(redis, key="k2", ttl_seconds=60, reply=reply)
    assert first is True
    assert second is False
    assert reply.await_count == 1


async def test_announce_once_falls_closed_when_redis_none() -> None:
    """No redis → no notification (vs. spamming every webhook)."""
    reply = AsyncMock(return_value=None)
    ok = await announce_once(None, key="k3", ttl_seconds=60, reply=reply)
    assert ok is False
    reply.assert_not_awaited()


async def test_announce_once_swallows_reply_errors() -> None:
    """Reply failures don't propagate — gate decision already made."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    reply = AsyncMock(side_effect=RuntimeError("github 502"))
    ok = await announce_once(redis, key="k4", ttl_seconds=60, reply=reply)
    assert ok is False  # reply failed → reported as not-sent


# ───── BLOCKED + announce_key dedup ─────


async def test_blocked_with_announce_key_posts_only_first_time() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    adapter = AsyncMock()
    adapter.reply = AsyncMock(return_value={"id": 1})
    ctx = _ctx(adapter=adapter, redis=redis)
    mw = _NamedMiddleware(
        "rate",
        MiddlewareDecision(
            result=MiddlewareResult.BLOCKED,
            reason="rate_user",
            comment="Rate limited",
            announce_key="rate:user:u:2026-05-15",
            announce_ttl=86400,
        ),
        [],
    )
    await run_preflight(ctx, [mw])
    await run_preflight(ctx, [mw])
    # Two BLOCKED runs → exactly one user-facing comment.
    assert adapter.reply.await_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("audit_phase", WorkflowPhase.SKIPPED),
        ("audit_phase", WorkflowPhase.REJECTED),
    ],
)
def test_middleware_decision_is_frozen(field: str, value: Any) -> None:
    """Decisions must be immutable — no in-place fixup after the runner returns."""
    decision = MiddlewareDecision(result=MiddlewareResult.BLOCKED, **{field: value})
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError
        setattr(decision, field, WorkflowPhase.FAILED)


# ───── unified sandbox entry: context carries handle + classifier ─────


def test_preflight_context_sandbox_handle_default_is_none() -> None:
    """Default construction leaves sandbox_handle unset — the dispatcher
    only attaches it on the REQUIRED path after a successful clone."""
    ctx = _ctx()
    assert ctx.sandbox_handle is None


def test_preflight_context_classifier_output_default_is_none() -> None:
    """No classifier ran yet → field stays None. Receive side populates
    it after the classifier returns (or after a fail-open None)."""
    ctx = _ctx()
    assert ctx.classifier_output is None


def test_preflight_context_replace_attaches_sandbox_handle() -> None:
    """The dispatcher threads a new SandboxedHandle through the context
    via ``dataclasses.replace`` — verify the immutability discipline
    works and unrelated fields survive the copy."""
    base = _ctx()
    handle = SandboxedHandle(
        sandbox=AsyncMock(),
        checkout=CheckoutSpec(
            repo_url="https://github.com/org/r.git",
            ref="main",
            strategy=CloneStrategy.SHALLOW,
        ),
        token="ghs_fake",
    )
    upgraded = dataclasses.replace(base, sandbox_handle=handle)
    # The new context carries the handle …
    assert upgraded.sandbox_handle is handle
    # … while the original stays untouched (frozen-dataclass guarantee).
    assert base.sandbox_handle is None
    # And the event / dispatch identity survives the copy.
    assert upgraded.event is base.event
    assert upgraded.dispatch is base.dispatch


def test_preflight_context_replace_attaches_classifier_output() -> None:
    """Same immutability discipline for the classifier signal — populated
    on the receive side before the policy gate runs."""
    base = _ctx()
    output = TriageClassifierOutput(
        type="bug",
        severity_guess="high",
        has_reproduction_info=True,
        looks_like_spam=False,
    )
    upgraded = dataclasses.replace(base, classifier_output=output)
    assert upgraded.classifier_output is output
    assert base.classifier_output is None


# ───── snapshot cache port: Task 2.1 ─────────────────────────────────────────


def test_preflight_context_sandbox_cache_default_is_none() -> None:
    """Default-constructed context has no cache backend wired.

    The dispatcher checks ``ctx.sandbox_cache is None`` to decide whether
    to attempt a warm-cache acquire. ``None`` means "no cache configured
    — always run the cold path". The NoOpSandboxCache is only used when
    wiring explicitly configures it; default wiring leaves this None
    so existing tests continue to exercise the cold path exclusively.
    """
    ctx = _ctx()
    assert ctx.sandbox_cache is None


def test_preflight_context_replace_sandbox_cache_preserves_other_fields() -> None:
    """``dataclasses.replace`` with a cache adapter must not disturb
    unrelated fields — frozen-dataclass immutability discipline.

    This mirrors the ``sandbox_handle`` / ``classifier_output`` tests: the
    DI layer swaps in the concrete adapter via ``replace``; everything
    else must be unchanged.
    """
    from openbot.infrastructure.sandboxes.cache_noop import NoOpSandboxCache

    base = _ctx()
    upgraded = dataclasses.replace(base, sandbox_cache=NoOpSandboxCache())

    assert upgraded.sandbox_cache is not None
    assert base.sandbox_cache is None  # original untouched
    assert upgraded.event is base.event
    assert upgraded.dispatch is base.dispatch
