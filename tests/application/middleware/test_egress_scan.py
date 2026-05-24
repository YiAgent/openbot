"""Egress scanner — verified-secret redaction + timeout fail-safe."""

from __future__ import annotations

from typing import Any

import pytest

from openbot.application.middleware.egress_scan import (
    REDACTION_MARKER,
    SAFE_TIMEOUT_REPLACEMENT,
    EgressScannedAdapter,
    EgressSurface,
    scan_egress_text,
)
from openbot.domain.events import EventKind, UnifiedEvent

# A high-entropy AWS-style stub. NOT a real key — `detect-secrets` flags
# the format, so the test asserts redaction, not a live secret check.
_FAKE_AWS_BLOB = "AKIAIOSFODNN7EXAMPLE secretkey=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_FAKE_KEY = "AKIAIOSFODNN7EXAMPLE"


async def test_clean_text_passes_through() -> None:
    result = await scan_egress_text("All clear here.", surface=EgressSurface.PR_REVIEW_BODY)
    assert result.text == "All clear here."
    assert result.findings == ()
    assert result.timed_out is False


async def test_redacts_aws_key_in_pr_review() -> None:
    result = await scan_egress_text(
        f"Reproduction:\n{_FAKE_AWS_BLOB}\nEnd.",
        surface=EgressSurface.PR_REVIEW_BODY,
    )
    assert REDACTION_MARKER in result.text
    # The AKIA prefix is the verified-secret detect-secrets flags as
    # ``AWS Access Key`` in default settings; redact replaces it.
    assert "AKIAIOSFODNN7EXAMPLE" not in result.text
    assert len(result.findings) >= 1


async def test_timeout_replaces_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    from openbot.application.middleware import egress_scan as mod

    def slow_scan(_text: str) -> list[mod._RawFinding]:
        # Simulate the scanner exceeding the soft timeout.
        import time

        time.sleep(0.6)
        return []

    monkeypatch.setattr(mod, "_run_detect_secrets", slow_scan)
    monkeypatch.setattr(mod, "_TIMEOUT_S", 0.05)
    result = await scan_egress_text(
        "anything " + _FAKE_AWS_BLOB,
        surface=EgressSurface.ISSUE_REPLY,
    )
    assert result.text == SAFE_TIMEOUT_REPLACEMENT
    assert result.timed_out is True


# ── Decorator tests ──


def _evt() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d1",
        kind=EventKind.PR_OPENED,
        repo="acme/web",
        actor="alice",
        installation_id=1,
        pr_number=42,
    )


class _RecordingAdapter:
    name = "recording"

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.review_bodies: list[str] = []
        self.inline_bodies: list[str] = []
        self.pr_titles: list[str] = []
        self.pr_bodies: list[str] = []

    async def reply(self, _event: UnifiedEvent, message: str) -> dict[str, Any]:
        self.replies.append(message)
        return {"id": 1}

    async def create_pr_review(
        self,
        _event: UnifiedEvent,
        _pr: int,
        *,
        body: str,
        event_type: str,
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.review_bodies.append(body)
        for c in comments or []:
            self.inline_bodies.append(c["body"])
        return {"id": 2}

    async def open_pull_request(
        self,
        _event: UnifiedEvent,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict[str, Any]:
        self.pr_titles.append(title)
        self.pr_bodies.append(body)
        return {"number": 99}


async def test_decorator_redacts_reply() -> None:
    inner = _RecordingAdapter()
    decorated = EgressScannedAdapter(inner, action="redact")
    await decorated.reply(_evt(), f"Hi: {_FAKE_KEY}")
    assert inner.replies, "reply must reach inner"
    assert "AKIAIOSFODNN7EXAMPLE" not in inner.replies[0]


async def test_decorator_redacts_review_body_and_inline() -> None:
    inner = _RecordingAdapter()
    decorated = EgressScannedAdapter(inner, action="redact")
    await decorated.create_pr_review(
        _evt(),
        42,
        body=f"Look: {_FAKE_KEY}",
        event_type="COMMENT",
        comments=[{"path": "a.py", "line": 1, "body": f"line: {_FAKE_KEY}"}],
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in inner.review_bodies[0]
    assert "AKIAIOSFODNN7EXAMPLE" not in inner.inline_bodies[0]


async def test_decorator_block_mode_replaces_review() -> None:
    inner = _RecordingAdapter()
    decorated = EgressScannedAdapter(inner, action="block")
    await decorated.create_pr_review(
        _evt(),
        42,
        body=f"Findings: {_FAKE_KEY}",
        event_type="COMMENT",
        comments=[{"path": "a.py", "line": 1, "body": "ok"}],
    )
    assert inner.review_bodies, "review still posted under block mode"
    assert "AKIAIOSFODNN7EXAMPLE" not in inner.review_bodies[0]
    # Block mode collapses the entire review body to a single audit note
    # and drops every inline comment, since any one of them might carry
    # the same secret.
    assert "openbot: response withheld" in inner.review_bodies[0].lower()
    assert inner.inline_bodies == []


async def test_decorator_passthrough_for_clean_text() -> None:
    inner = _RecordingAdapter()
    decorated = EgressScannedAdapter(inner, action="redact")
    await decorated.reply(_evt(), "Nothing to see here.")
    assert inner.replies == ["Nothing to see here."]
