"""Unit tests for openbot.domain.review.

Tests: passes_threshold boundary table, Finding immutability,
ReviewFindings empty-findings default, and unknown-severity resilience.
"""

from __future__ import annotations

import pytest

from openbot.domain.review import Finding, ReviewFindings, passes_threshold


def _finding(severity: str = "medium", file: str = "foo.py") -> Finding:
    return Finding(severity=severity, file=file, message="test finding")  # type: ignore[arg-type]


# ── passes_threshold table ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestPassesThreshold:
    @pytest.mark.parametrize(
        "severity, threshold, expected",
        [
            # critical passes everything
            ("critical", "critical", True),
            ("critical", "high", True),
            ("critical", "medium", True),
            ("critical", "low", True),
            # medium passes medium and above
            ("medium", "medium", True),
            ("medium", "high", False),
            # low passes only low
            ("low", "low", True),
            ("low", "medium", False),
            # nit fails at medium threshold
            ("nit", "medium", False),
            # high passes medium
            ("high", "medium", True),
        ],
    )
    def test_boundary(self, severity: str, threshold: str, expected: bool) -> None:
        finding = _finding(severity=severity)
        assert passes_threshold(finding, threshold) is expected  # type: ignore[arg-type]

    def test_unknown_severity_returns_false(self) -> None:
        """LLM hallucinated severity must not crash — silently dropped."""
        finding = _finding(severity="catastrophic")
        assert passes_threshold(finding, "medium") is False  # type: ignore[arg-type]

    def test_unknown_severity_any_threshold_returns_false(self) -> None:
        finding = _finding(severity="blocker")
        for threshold in ("critical", "high", "medium", "low"):
            assert passes_threshold(finding, threshold) is False  # type: ignore[arg-type]


# ── Finding immutability ───────────────────────────────────────────────────────


@pytest.mark.unit
class TestFindingImmutability:
    def test_frozen_raises_on_mutation(self) -> None:
        finding = _finding()
        with pytest.raises((AttributeError, TypeError)):
            finding.severity = "critical"  # type: ignore[misc]

    def test_optional_fields_default_to_none(self) -> None:
        finding = _finding()
        assert finding.line is None
        assert finding.quote is None


# ── ReviewFindings ─────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestReviewFindings:
    def test_empty_findings_by_default(self) -> None:
        rf = ReviewFindings(summary="lgtm")
        assert rf.findings == ()

    def test_findings_stored_as_tuple(self) -> None:
        f = _finding()
        rf = ReviewFindings(summary="issues found", findings=(f,))
        assert isinstance(rf.findings, tuple)
        assert rf.findings[0] is f

    def test_frozen_raises_on_mutation(self) -> None:
        rf = ReviewFindings(summary="lgtm")
        with pytest.raises((AttributeError, TypeError)):
            rf.summary = "changed"  # type: ignore[misc]
