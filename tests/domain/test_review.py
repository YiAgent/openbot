"""Tests for openbot.domain.review — frozen value objects for PR review findings."""

from __future__ import annotations

import pytest

from openbot.domain.review import Finding, ReviewFindings, passes_threshold

# ───── construction / immutability ──────────────────────────────────────────


def test_finding_is_frozen() -> None:
    f = Finding(severity="high", file="x.py", message="bug")
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        f.severity = "low"  # type: ignore[misc]


def test_finding_optional_fields_default_to_none() -> None:
    f = Finding(severity="medium", file="x.py", message="msg")
    assert f.line is None
    assert f.quote is None


def test_review_findings_is_frozen_and_defaults_findings_to_empty_tuple() -> None:
    r = ReviewFindings(summary="LGTM")
    assert r.findings == ()
    with pytest.raises((AttributeError, Exception)):
        r.summary = "nope"  # type: ignore[misc]


def test_review_findings_carries_findings_tuple() -> None:
    f1 = Finding(severity="critical", file="a.py", message="m1", line=10)
    f2 = Finding(severity="nit", file="b.py", message="m2")
    r = ReviewFindings(summary="2 issues", findings=(f1, f2))
    assert r.findings == (f1, f2)


# ───── passes_threshold ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "severity, threshold, expected",
    [
        # threshold=medium keeps {critical, high, medium}; drops {low, nit}
        ("critical", "medium", True),
        ("high", "medium", True),
        ("medium", "medium", True),
        ("low", "medium", False),
        ("nit", "medium", False),
        # threshold=critical keeps only critical
        ("critical", "critical", True),
        ("high", "critical", False),
        ("medium", "critical", False),
        # threshold=low keeps everything except nit
        ("critical", "low", True),
        ("low", "low", True),
        ("nit", "low", False),
    ],
)
def test_passes_threshold_rankings(severity: str, threshold: str, expected: bool) -> None:
    f = Finding(severity=severity, file="x.py", message="m")  # type: ignore[arg-type]
    assert passes_threshold(f, threshold) is expected  # type: ignore[arg-type]


def test_passes_threshold_rejects_unknown_severity() -> None:
    # An LLM might invent severities; passes_threshold should not crash, it should drop.
    # We deliberately bypass the type system here — runtime guard matters.
    f = Finding(severity="catastrophic", file="x.py", message="m")  # type: ignore[arg-type]
    assert passes_threshold(f, "medium") is False  # type: ignore[arg-type]
