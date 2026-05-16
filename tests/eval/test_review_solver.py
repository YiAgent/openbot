"""Unit tests for evals.solvers.review — P1 regression pins.

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

from evals.solvers.review import (
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
        "evals.common.deepagents_baseline.create_deep_agent",
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

    with patch("evals.common.deepagents_baseline.create_deep_agent", return_value=_Agent()):
        result = review_diff("noop diff")

    assert "PWNED-block-1" in result.raw_text
    assert result.findings == []


# ─── Structured-output path: deepagents `response_format=` ──────────────────


def test_review_diff_consumes_structured_response_over_regex() -> None:
    """When deepagents emits ``structured_response``, the regex extractor is bypassed.

    Pins the deepagents 0.6.1 ``response_format=`` contract: the parsed
    Pydantic model on ``result['structured_response']`` is the authoritative
    findings source. The regex fallback only fires when that field is absent
    (e.g. legacy stub agents). Without this pin, a schema-valid response that
    happens to contain conflicting JSON in prose could silently corrupt
    scoring.
    """
    from evals.solvers.review import _ReviewResponseModel

    class _AiMsg:
        type = "ai"

        def __init__(self) -> None:
            # Prose carries a DECOY JSON object — must be ignored in favor of
            # the structured_response below.
            self.content = 'Decoy findings: {"findings": [{"file":"decoy.py","line":1,"body":"x","severity":"low"}]}'

    structured = _ReviewResponseModel(
        findings=[
            {"file": "real.py", "line": 42, "body": "actual bug", "severity": "high"},  # type: ignore[list-item]
        ]
    )

    class _Agent:
        def invoke(self, _payload):
            return {"messages": [_AiMsg()], "structured_response": structured}

    with patch("evals.common.deepagents_baseline.create_deep_agent", return_value=_Agent()):
        result = review_diff("diff stub")

    # Structured findings win; decoy JSON in prose is ignored.
    assert len(result.findings) == 1
    assert result.findings[0]["file"] == "real.py"
    assert result.findings[0]["severity"] == "high"
    # Raw text still carries the decoy so the safety scorer can inspect it.
    assert "decoy.py" in result.raw_text


def test_review_diff_concatenates_all_ai_messages_for_safety_scan() -> None:
    """The raw-text surface must span EVERY AI turn, not just the last one.

    With ``response_format=`` set, deepagents emits a schema-binding terminal
    step as its own AI message. An attacker who plants a canary in an earlier
    chain-of-thought turn would slip past a scan that only looked at
    ``messages[-1]``. This test pins the multi-turn join.
    """

    class _AiMsg:
        type = "ai"

        def __init__(self, content: str) -> None:
            self.content = content

    class _Agent:
        def invoke(self, _payload):
            return {
                "messages": [
                    _AiMsg("Reasoning step with canary PWNED-early-turn."),
                    _AiMsg("Final structured step."),
                ],
                "structured_response": None,
            }

    with patch("evals.common.deepagents_baseline.create_deep_agent", return_value=_Agent()):
        result = review_diff("diff stub")

    assert "PWNED-early-turn" in result.raw_text
    assert "Final structured step." in result.raw_text


# ─── Sandbox-mode `@solver` path ───────────────────────────────────────────


def test_sandbox_user_message_surfaces_pr_metadata_for_gh() -> None:
    """Sandbox-mode user message must carry repo / pr_url / pr_title / base_sha.

    The deepagents agent uses these identifiers verbatim in ``gh pr view`` /
    ``gh api`` / ``gh repo clone`` calls. If we strip them out, the agent
    can't autonomously fetch extra context, defeating the whole sandbox
    upgrade. Pins the contract so future prompt edits don't regress it.
    """
    from evals.solvers.review import _build_sandbox_user_message

    msg = _build_sandbox_user_message(
        diff="diff --git a/x b/x\n+x\n",
        repo="owner/repo",
        pr_url="https://github.com/owner/repo/pull/42",
        pr_title="Fix the thing",
        base_sha="deadbeef",
    )
    assert "owner/repo" in msg
    assert "https://github.com/owner/repo/pull/42" in msg
    assert "Fix the thing" in msg
    assert "deadbeef" in msg
    # Diff still carried inline so the cheap path needs zero shell calls.
    assert "diff --git a/x b/x" in msg


def test_sandbox_user_message_omits_missing_pr_metadata() -> None:
    """Header lines drop out cleanly when their metadata key is absent."""
    from evals.solvers.review import _build_sandbox_user_message

    msg = _build_sandbox_user_message(
        diff="x", repo=None, pr_url=None, pr_title=None, base_sha=None
    )
    assert "Repository:" not in msg
    assert "PR URL:" not in msg
    assert "Base commit:" not in msg
    # Diff still rendered.
    assert "```diff\nx\n```" in msg


def test_deepagents_baseline_review_solver_closed_form_round_trip() -> None:
    """``use_sandbox=False`` flows through the pure ``review_diff`` path.

    Guards against accidental regressions where someone removes the
    closed-form escape hatch — that path stays valuable for sandbox-less
    CI tiers and for the regression tests above.
    """
    import asyncio

    from evals.solvers.review import deepagents_baseline_review_solver

    class _AiMsg:
        type = "ai"

        def __init__(self, content: str) -> None:
            self.content = content

    class _Agent:
        def invoke(self, _payload):
            return {
                "messages": [_AiMsg('{"findings": []}')],
                "structured_response": None,
            }

    class _State:
        def __init__(self) -> None:
            self.input_text = "diff stub"
            self.sample_id = "s1"
            self.metadata: dict = {}
            self.output = type("O", (), {"completion": ""})()

    with patch("evals.common.deepagents_baseline.create_deep_agent", return_value=_Agent()):
        solver = deepagents_baseline_review_solver(use_sandbox=False)
        state = _State()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(solver(state, None))  # type: ignore[arg-type]
        finally:
            loop.close()

    assert state.metadata["candidate_findings"] == []
    assert "findings" in state.metadata["candidate_findings_json"]
    assert state.output.completion == '{"findings": []}'


def test_deepagents_baseline_review_solver_separates_raw_output_from_completion() -> None:
    """Raw agent prose stays in metadata; completion stays structured for exports."""
    import asyncio

    from evals.solvers.review import deepagents_baseline_review_solver

    class _AiMsg:
        type = "ai"

        def __init__(self, content: str) -> None:
            self.content = content

    class _Agent:
        def invoke(self, _payload):
            return {
                "messages": [
                    _AiMsg("Let me read the key files to understand the full context."),
                    _AiMsg('{"findings": []}'),
                ],
                "structured_response": None,
            }

    class _State:
        def __init__(self) -> None:
            self.input_text = "diff stub"
            self.sample_id = "s1"
            self.metadata: dict = {}
            self.output = type("O", (), {"completion": ""})()

    with patch("evals.common.deepagents_baseline.create_deep_agent", return_value=_Agent()):
        solver = deepagents_baseline_review_solver(use_sandbox=False)
        state = _State()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(solver(state, None))  # type: ignore[arg-type]
        finally:
            loop.close()

    assert "Let me read the key files" in state.metadata["agent_raw_output"]
    assert state.output.completion == '{"findings": []}'


def test_deepagents_baseline_review_solver_uses_shared_deepagents_model_by_default(
    monkeypatch,
) -> None:
    """Review solver should inherit the repo-wide shared deepagents model env."""
    import asyncio

    from evals.solvers import review as review_mod

    seen: dict[str, str] = {}

    def _fake_review_diff(diff: str, *, model: str) -> ReviewResult:
        seen["diff"] = diff
        seen["model"] = model
        return ReviewResult(raw_text='{"findings": []}', findings=[])

    class _State:
        def __init__(self) -> None:
            self.input_text = "diff stub"
            self.sample_id = "s1"
            self.metadata: dict = {}
            self.output = type("O", (), {"completion": ""})()

    monkeypatch.delenv("OPENBOT_REVIEW_MODEL_ID", raising=False)
    monkeypatch.setenv("OPENBOT_DEEPAGENTS_MODEL", "mimo-v2.5")
    monkeypatch.setattr(review_mod, "review_diff", _fake_review_diff)

    solver = review_mod.deepagents_baseline_review_solver(use_sandbox=False)
    state = _State()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(solver(state, None))  # type: ignore[arg-type]
    finally:
        loop.close()

    assert seen["diff"] == "diff stub"
    assert seen["model"] == "anthropic:mimo-v2.5"
