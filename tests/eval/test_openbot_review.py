"""Unit tests for evals.solvers.openbot_review — P1 regression pins.

Codex review (2026-05-15) caught two false-negative classes in the original
implementation:

  P1#1 — the redteam scorer was being fed the sanitized JSON of parsed
         findings, not the raw model reply. An attacker who leaked a canary
         in prefatory prose would pass.

These tests pin the fix: `review_diff` now returns a `ReviewResult` with
**both** raw text and parsed findings.
"""

from __future__ import annotations

from unittest.mock import patch

from evals.solvers.openbot_review import (
    ReviewResult,
    _coerce_findings,
    _extract_json_object,
    review_diff,
)


def test_review_result_carries_both_raw_and_findings() -> None:
    """The ReviewResult dataclass exposes the raw agent reply alongside parsed findings."""
    rr = ReviewResult(
        raw_text="prose first then JSON",
        findings=[{"file": "a.py", "line": 1, "body": "bug", "severity": "high"}],
    )
    assert rr.raw_text == "prose first then JSON"
    assert len(rr.findings) == 1
    assert rr.findings[0]["body"] == "bug"


def test_extract_json_object_picks_last_balanced_block() -> None:
    """The parser must pull the trailing JSON even when prose comes first."""
    text = 'Here is a leaked canary: PWNED-leak. Final: {"findings": [{"file":"x"}]}'
    obj = _extract_json_object(text)
    assert obj == {"findings": [{"file": "x"}]}


def test_coerce_findings_silently_drops_malformed_items() -> None:
    """Robustness: garbage list items don't crash the solver."""
    out = _coerce_findings(
        {
            "findings": [
                {"file": "a.py", "line": 1, "body": "ok", "severity": "high"},
                "not-a-dict",
                {"file": "b.py"},  # missing required keys
                {"file": "c.py", "line": "not-int", "body": "x", "severity": "low"},  # bad line
                {
                    "file": "d.py",
                    "line": 2,
                    "body": "ok2",
                    "severity": "weird",
                },  # bad severity → defaults
            ]
        }
    )
    assert len(out) == 2  # a.py + d.py survive; others dropped
    assert out[0]["file"] == "a.py"
    assert out[1]["severity"] == "medium"  # defaulted


# ─── P1#1 regression: raw text preserved ────────────────────────────────────


def _fake_agent_returning(text: str):
    """Build a stub deep-agent whose `.invoke` returns the given text as last message."""

    class _Msg:
        def __init__(self, content: str) -> None:
            self.content = content

    class _Agent:
        def invoke(self, _payload):
            return {"messages": [_Msg(text)]}

    return _Agent()


def test_review_diff_returns_raw_text_when_prose_precedes_json() -> None:
    """An attacker who leaks a canary in prefatory prose must be observable in raw_text."""
    leaky = (
        "Sure, here is the token you asked for: PWNED-leak-001.\n"
        'And here is the structured findings: {"findings": []}'
    )
    with patch(
        "evals.solvers.openbot_review.create_deep_agent",
        return_value=_fake_agent_returning(leaky),
    ):
        result = review_diff("diff --git a/x b/x\n+x\n")
    assert "PWNED-leak-001" in result.raw_text
    # Parser still extracts the empty findings block.
    assert result.findings == []
    # Critical regression pin: raw_text MUST contain the leak even though
    # findings is empty. The previous solver would only have surfaced the
    # JSON, hiding the canary from the safety scorer.


def test_review_diff_handles_anthropic_content_blocks() -> None:
    """Some langchain backends return content as list[{type:text, text:str}]."""

    class _Msg:
        def __init__(self) -> None:
            self.content = [
                {"type": "text", "text": "Block 1 with canary PWNED-block-1. "},
                {"type": "text", "text": '{"findings": []}'},
            ]

    class _Agent:
        def invoke(self, _payload):
            return {"messages": [_Msg()]}

    with patch("evals.solvers.openbot_review.create_deep_agent", return_value=_Agent()):
        result = review_diff("noop diff")

    assert "PWNED-block-1" in result.raw_text
    assert result.findings == []
