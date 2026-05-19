"""Cancel middlewares — kill switch + cancel label + cancel comment."""

from __future__ import annotations

from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from openbot.application.middleware import MiddlewareResult
from openbot.application.middleware.cancel import (
    CancelCommentMiddleware,
    CancelLabelMiddleware,
    KillSwitchMiddleware,
)
from openbot.domain.events import EventKind
from openbot.infrastructure.llm.model_router import Feature
from openbot.infrastructure.persistence.models import WorkflowPhase
from tests.application.middleware.conftest import make_ctx, make_event

# ───── KillSwitchMiddleware ─────


async def test_kill_switch_blocks_when_env_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_KILL_SWITCH", "true")
    decision = await KillSwitchMiddleware()(make_ctx())
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "kill_switch"
    assert decision.audit_phase is WorkflowPhase.REJECTED
    assert decision.comment is None  # admin job, no user-facing reply


async def test_kill_switch_proceeds_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENBOT_KILL_SWITCH", raising=False)
    decision = await KillSwitchMiddleware()(make_ctx())
    assert decision.result is MiddlewareResult.PROCEED


async def test_kill_switch_proceeds_on_non_true_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the literal string `true` (case-insensitive) trips."""
    for value in ["false", "0", "yes", "TRUE"]:
        monkeypatch.setenv("OPENBOT_KILL_SWITCH", value)
        decision = await KillSwitchMiddleware()(make_ctx())
        if value.lower() == "true":
            assert decision.result is MiddlewareResult.BLOCKED, value
        else:
            assert decision.result is MiddlewareResult.PROCEED, value


# ───── CancelLabelMiddleware ─────


async def test_cancel_label_blocks_when_label_present() -> None:
    adapter = AsyncMock()
    adapter._api_base = "https://api.github.com"
    adapter._authed_json = AsyncMock(return_value=[{"name": "bug"}, {"name": "cancel-openbot"}])
    decision = await CancelLabelMiddleware()(make_ctx(adapter=adapter))
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "cancel_label"
    assert decision.comment is None  # user already signaled intent
    adapter._authed_json.assert_awaited_once()


async def test_cancel_label_proceeds_when_label_absent() -> None:
    adapter = AsyncMock()
    adapter._api_base = "https://api.github.com"
    adapter._authed_json = AsyncMock(return_value=[{"name": "bug"}])
    decision = await CancelLabelMiddleware()(make_ctx(adapter=adapter))
    assert decision.result is MiddlewareResult.PROCEED


async def test_cancel_label_caches_labels_on_ctx() -> None:
    """Second call within same request reuses the labels list."""
    adapter = AsyncMock()
    adapter._api_base = "https://api.github.com"
    adapter._authed_json = AsyncMock(return_value=[{"name": "bug"}])
    ctx = make_ctx(adapter=adapter)
    await CancelLabelMiddleware()(ctx)
    await CancelLabelMiddleware()(ctx)
    # Even two passes → one API call. Cache hit on second.
    adapter._authed_json.assert_awaited_once()


async def test_cancel_label_fall_open_on_api_error() -> None:
    """A 502 from GitHub must NOT block all webhooks indefinitely."""
    adapter = AsyncMock()
    adapter._api_base = "https://api.github.com"
    adapter._authed_json = AsyncMock(side_effect=RuntimeError("502"))
    decision = await CancelLabelMiddleware()(make_ctx(adapter=adapter))
    assert decision.result is MiddlewareResult.PROCEED


async def test_cancel_label_proceeds_when_no_issue_or_pr() -> None:
    """Events with no issue/PR surface (push, release) can't carry the label."""
    event = make_event(kind=EventKind.UNKNOWN, issue_number=None, pr_number=None)
    decision = await CancelLabelMiddleware()(make_ctx(event=event))
    assert decision.result is MiddlewareResult.PROCEED


# ───── CancelCommentMiddleware ─────


@pytest.mark.parametrize(
    "comment_body,expected_block",
    [
        ("@openbot stop", True),
        ("@openbot cancel", True),
        ("@openbot 停", True),
        ("@openbot 取消", True),
        ("@openbot Stop everything", True),  # case-insensitive on keyword
        ("@openbot help me", False),  # different command word
        ("hello @openbot stop", False),  # not at start of message
        ("@openbot-dev stop", False),  # different bot login
        ("", False),
    ],
)
async def test_cancel_comment_matches_only_known_keywords(
    comment_body: str, expected_block: bool
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body=comment_body)
    ctx = make_ctx(event=event, feature=Feature.CHAT, redis=redis)
    decision = await CancelCommentMiddleware()(ctx)
    if expected_block:
        assert decision.result is MiddlewareResult.BLOCKED, comment_body
        assert decision.reason == "cancel_comment"
        # §9.4: always replies, no announce_key dedup.
        assert decision.comment is not None
        assert decision.announce_key is None
    else:
        assert decision.result is MiddlewareResult.PROCEED, comment_body


async def test_cancel_comment_skips_non_chat_features() -> None:
    """Triage / review / fix have no chat-comment surface to cancel on."""
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body="@openbot stop")
    ctx = make_ctx(event=event, feature=Feature.TRIAGE)
    decision = await CancelCommentMiddleware()(ctx)
    assert decision.result is MiddlewareResult.PROCEED


async def test_cancel_comment_seeds_cancel_thread_in_redis() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body="@openbot stop")
    ctx = make_ctx(event=event, feature=Feature.CHAT, redis=redis)
    await CancelCommentMiddleware()(ctx)
    # SADD record visible afterwards.
    members = await redis.smembers(f"cancel:thread:{ctx.dispatch.task_id}")
    assert members == {"1"}


async def test_cancel_comment_redis_missing_still_blocks() -> None:
    """Even without Redis the cancel still BLOCKS (just no SADD)."""
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body="@openbot cancel")
    decision = await CancelCommentMiddleware()(
        make_ctx(event=event, feature=Feature.CHAT, redis=None)
    )
    assert decision.result is MiddlewareResult.BLOCKED


async def test_cancel_comment_strips_zero_width_format_chars() -> None:
    """`@openbot​stop` with U+200B ZWSP between mention and verb still trips."""
    body = "@openbot ​stop"  # ZWSP, category Cf
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body=body)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    decision = await CancelCommentMiddleware()(
        make_ctx(event=event, feature=Feature.CHAT, redis=redis)
    )
    assert decision.result is MiddlewareResult.BLOCKED


async def test_cancel_comment_caps_body_length_for_redos_safety() -> None:
    """A 50-MB payload must not stall the regex engine."""
    body = "@openbot " + ("x" * 50_000_000)
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body=body)
    # Should return quickly with a PROCEED — `x` isn't a cancel keyword.
    decision = await CancelCommentMiddleware()(make_ctx(event=event, feature=Feature.CHAT))
    assert decision.result is MiddlewareResult.PROCEED
