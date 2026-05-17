"""SanitizeInputsMiddleware — chain entry byte gate."""

from __future__ import annotations

import logging

import pytest

from openbot.events import EventKind
from openbot.middleware import MiddlewareResult
from openbot.middleware.sanitize import (
    SanitizeInputsMiddleware,
    sanitized_event,
)
from openbot.persistence.models import WorkflowPhase
from tests.middleware.conftest import make_ctx, make_event

# ───── invisible-byte stripping ─────


# Source-file rule: NEVER embed literal Cf/RTL characters here, even
# inside a string. ``semgrep`` flags bidi codepoints in source as a
# review-time risk (CWE-94) regardless of whether the surrounding code
# treats them as data. We synthesize them via ``chr(...)`` so this
# file stays ASCII-only.
_ZWSP = chr(0x200B)  # ZERO WIDTH SPACE   (general category Cf)
_RLO = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE (general category Cf)


async def test_strips_zero_width_space_from_comment_body() -> None:
    """ZWSP (U+200B, category Cf) between mention and verb is removed."""
    body = f"@openbot{_ZWSP}stop"
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body=body)
    ctx = make_ctx(event=event)
    decision = await SanitizeInputsMiddleware()(ctx)
    assert decision.result is MiddlewareResult.PROCEED
    cleaned = sanitized_event(ctx).comment_body
    assert cleaned == "@openbotstop"
    # ``ctx.event`` itself is untouched (frozen dataclass).
    assert ctx.event.comment_body == body


async def test_strips_rtl_override() -> None:
    """U+202E (RIGHT-TO-LEFT OVERRIDE, Cf) is removed."""
    body = f"@openbot {_RLO}stop"
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body=body)
    ctx = make_ctx(event=event)
    await SanitizeInputsMiddleware()(ctx)
    assert _RLO not in (sanitized_event(ctx).comment_body or "")


async def test_preserves_newlines_and_tabs() -> None:
    """``\\n`` and ``\\t`` are control characters but carry meaning."""
    body = "line1\nline2\twith-tab"
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body=body)
    ctx = make_ctx(event=event)
    await SanitizeInputsMiddleware()(ctx)
    assert sanitized_event(ctx).comment_body == body


# ───── actor regex gate ─────


async def test_actor_invalid_blocks_with_path_traversal_attempt() -> None:
    """Actors that include ``/`` or ``..`` (Redis-key injection vectors) → BLOCKED."""
    event = make_event(actor="../etc/passwd")
    decision = await SanitizeInputsMiddleware()(make_ctx(event=event))
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "actor_invalid"
    assert decision.audit_phase is WorkflowPhase.REJECTED
    # No reply for forged actors — would echo the attacker-supplied string.
    assert decision.comment is None


async def test_actor_invalid_blocks_on_control_char() -> None:
    """Newline in actor would split the Redis key into two keys."""
    event = make_event(actor="alice\nbob")
    decision = await SanitizeInputsMiddleware()(make_ctx(event=event))
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "actor_invalid"


@pytest.mark.parametrize(
    "actor",
    [
        "alice",
        "Alice-Wonder",
        "user-123",
        "a" * 39,  # max GitHub login length
    ],
)
async def test_actor_valid_passes(actor: str) -> None:
    event = make_event(actor=actor)
    decision = await SanitizeInputsMiddleware()(make_ctx(event=event))
    assert decision.result is MiddlewareResult.PROCEED


async def test_actor_empty_passes() -> None:
    """Empty actor (system-fired event, replay) → no regex match, just PROCEED."""
    event = make_event(actor="")
    decision = await SanitizeInputsMiddleware()(make_ctx(event=event))
    assert decision.result is MiddlewareResult.PROCEED


# ───── length caps ─────


async def test_single_field_truncated_logs_field_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """8 KB overflow → truncate (PROCEED), structured log records field name."""
    body = "x" * 9_000
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body=body)
    ctx = make_ctx(event=event)
    with caplog.at_level(logging.INFO, logger="openbot.middleware.sanitize"):
        decision = await SanitizeInputsMiddleware()(ctx)
    assert decision.result is MiddlewareResult.PROCEED
    cleaned = sanitized_event(ctx).comment_body
    assert cleaned is not None
    assert len(cleaned) == 8_000
    truncate_records = [r for r in caplog.records if r.message == "sanitize_field_truncated"]
    assert truncate_records
    assert "comment_body" in getattr(truncate_records[0], "fields", [])


async def test_total_too_large_blocks() -> None:
    """Total > 64 KB total cap → BLOCKED reason=input_too_large, REJECTED."""
    # Single field test — comment_body alone with no Cf chars; we
    # raise the per-field cap so it doesn't truncate first, leaving
    # the total-length cap as the gate under test.
    body = "x" * 100_000
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body=body)
    decision = await SanitizeInputsMiddleware(
        max_field_length=200_000,  # disable per-field truncation
        max_total_length=64_000,
    )(make_ctx(event=event))
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "input_too_large"
    assert decision.audit_phase is WorkflowPhase.REJECTED


# ───── cache contract ─────


async def test_sanitized_event_available_downstream() -> None:
    """Subsequent middlewares + workflows read the sanitized version."""
    body = "@openbot​ help"
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body=body)
    ctx = make_ctx(event=event)
    await SanitizeInputsMiddleware()(ctx)
    assert sanitized_event(ctx).comment_body == "@openbot help"


async def test_sanitized_event_falls_back_to_raw_when_middleware_did_not_run() -> None:
    """If the chain skipped sanitize, the helper returns the raw event."""
    event = make_event(kind=EventKind.ISSUE_COMMENT_CREATED, comment_body="hello")
    ctx = make_ctx(event=event)
    # No middleware invocation → no cache entry.
    assert sanitized_event(ctx) is event
