"""Egress scanner — verified-secret redaction + timeout fail-safe."""

from __future__ import annotations

import pytest

from openbot.application.middleware.egress_scan import (
    REDACTION_MARKER,
    SAFE_TIMEOUT_REPLACEMENT,
    EgressSurface,
    scan_egress_text,
)

# A high-entropy AWS-style stub. NOT a real key — `detect-secrets` flags
# the format, so the test asserts redaction, not a live secret check.
_FAKE_AWS_BLOB = "AKIAIOSFODNN7EXAMPLE secretkey=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


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
