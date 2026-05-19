"""Chat mention parser — harness spec §3 M11."""

from __future__ import annotations

import pytest

from openbot.application.workflows.chat_parser import ChatCommand, parse

# ───── happy paths ─────


def test_freeform_request_parses() -> None:
    cmd = parse("@openbot can you reproduce issue #42?")
    assert isinstance(cmd, ChatCommand)
    assert cmd.is_freeform is True
    assert cmd.is_cancel is False
    assert cmd.is_help is False
    assert cmd.body_after_mention == "can you reproduce issue #42?"


@pytest.mark.parametrize(
    "body,expected_cancel,expected_help",
    [
        ("@openbot stop", True, False),
        ("@openbot cancel", True, False),
        ("@openbot 停", True, False),
        ("@openbot 取消", True, False),
        ("@openbot STOP everything", True, False),  # case-insensitive
        ("@openbot help", False, True),
        ("@openbot ?", False, True),
        ("@openbot 帮助", False, True),
    ],
)
def test_keywords_classified(body: str, expected_cancel: bool, expected_help: bool) -> None:
    cmd = parse(body)
    assert cmd is not None
    assert cmd.is_cancel is expected_cancel, body
    assert cmd.is_help is expected_help, body


def test_bare_mention_treated_as_empty_freeform() -> None:
    """`@openbot` alone (no body) is a freeform ping."""
    cmd = parse("@openbot")
    assert cmd is not None
    assert cmd.is_freeform is True
    assert cmd.body_after_mention == ""


# ───── rejection paths (return None, not raise) ─────


@pytest.mark.parametrize(
    "body",
    [
        "",
        None,
        "hey @openbot stop",  # not at start of message
        "@openbot-dev stop",  # different bot login
        "  please @openbot help",  # leading space + body
    ],
)
def test_non_mentions_return_none(body) -> None:
    assert parse(body) is None


# ───── safety ─────


def test_zero_width_space_stripped_before_match() -> None:
    """`@openbot​stop` (U+200B ZWSP between mention and verb) must still trip."""
    body = "@openbot ​stop"
    cmd = parse(body)
    assert cmd is not None
    assert cmd.is_cancel is True


def test_oversized_body_capped_safely() -> None:
    """A 50-MB paste must NOT stall the regex engine."""
    body = "@openbot " + ("x" * 50_000_000)
    # Returns quickly. The truncated body still parses; `x` isn't a
    # cancel/help keyword so it's classified as freeform.
    cmd = parse(body)
    assert cmd is not None
    assert cmd.is_freeform is True
    # Body length is bounded — well below the original input size.
    assert len(cmd.body_after_mention) < 5_000


def test_dataclass_is_frozen() -> None:
    """Parsed commands must be immutable so the workflow can't tamper
    mid-flight."""
    cmd = parse("@openbot help")
    assert cmd is not None
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError
        cmd.is_cancel = True  # type: ignore[misc]


def test_cancel_keyword_must_be_first_token() -> None:
    """`@openbot please don't stop` is freeform, not cancel — `stop` isn't
    the immediate command word."""
    cmd = parse("@openbot please don't stop yet")
    assert cmd is not None
    assert cmd.is_cancel is False
    assert cmd.is_freeform is True
