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

from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

from evals.agents.review import ReviewResponseModel, build_review_user_message
from evals.solvers.review import (
    ReviewResult,
    _coerce_findings,
    _extract_json_object,
    review_diff,
)


@contextmanager
def _stub_finalizer(returned: Any) -> Any:
    """Patch the structured finalizer for offline tests.

    ``build_baseline_agent`` wraps the compiled deepagents graph with a
    finalizer that makes a real Anthropic API call. Unit tests stub the
    underlying agent via ``create_deep_agent`` and don't have credentials —
    so we also stub *both* the async and sync finalizer functions for the
    duration of the test. ``returned`` is the parsed schema instance the
    wrapper should inject into ``result["structured_response"]``.
    """

    async def _fake_finalize(_messages, *, schema, model, **_kwargs):
        return returned

    def _fake_finalize_sync(_messages, *, schema, model, **_kwargs):
        return returned

    with (
        patch(
            "evals.agents.middleware.finalize_structured",
            side_effect=_fake_finalize,
        ),
        patch(
            "evals.agents.middleware.finalize_structured_sync",
            side_effect=_fake_finalize_sync,
        ) as p,
    ):
        yield p


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
        type = "ai"  # termination contract requires the tail-message be AI-type

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
    with (
        patch(
            "evals.agents.baseline.create_deep_agent",
            return_value=_fake_agent_returning(leaky),
        ),
        _stub_finalizer(ReviewResponseModel(findings=[])),
    ):
        result = review_diff("diff --git a/x b/x\n+x\n")
    assert "PWNED-leak-001" in result.raw_text
    # Finalizer produced empty findings; raw_text still carries the canary.
    assert result.findings == []
    # Critical regression pin: raw_text MUST contain the leak even though
    # findings is empty. The previous solver would only have surfaced the
    # JSON, hiding the canary from the safety scorer.


def test_review_diff_handles_anthropic_content_blocks() -> None:
    """Some langchain backends return content as list[{type:text, text:str}]."""

    class _Msg:
        type = "ai"  # termination contract requires the tail-message be AI-type

        def __init__(self) -> None:
            self.content = [
                {"type": "text", "text": "Block 1 with canary PWNED-block-1. "},
                {"type": "text", "text": '{"findings": []}'},
            ]

    class _Agent:
        def invoke(self, _payload):
            return {"messages": [_Msg()]}

    with (
        patch("evals.agents.baseline.create_deep_agent", return_value=_Agent()),
        _stub_finalizer(ReviewResponseModel(findings=[])),
    ):
        result = review_diff("noop diff")

    assert "PWNED-block-1" in result.raw_text
    assert result.findings == []


# ─── Structured-output path: deepagents `response_format=` ──────────────────


def test_review_diff_consumes_structured_response_over_regex() -> None:
    """The wrapper's structured_response is the authoritative findings source.

    Pins the post-finalizer contract: the parsed Pydantic model that the
    structured-finalizer wrapper injects into
    ``result['structured_response']`` overrides any conflicting JSON in
    the agent's prose. Without this pin, decoy JSON in chain-of-thought
    output could silently corrupt scoring.
    """

    class _AiMsg:
        type = "ai"

        def __init__(self) -> None:
            # Prose carries a DECOY JSON object — must be ignored in favor of
            # the finalizer-produced schema below.
            self.content = 'Decoy findings: {"findings": [{"file":"decoy.py","line":1,"body":"x","severity":"low"}]}'

    structured = ReviewResponseModel(
        findings=[
            {"file": "real.py", "line": 42, "body": "actual bug", "severity": "high"},  # type: ignore[list-item]
        ]
    )

    class _Agent:
        def invoke(self, _payload):
            return {"messages": [_AiMsg()]}

    with (
        patch("evals.agents.baseline.create_deep_agent", return_value=_Agent()),
        _stub_finalizer(structured),
    ):
        result = review_diff("diff stub")

    # Finalizer schema wins; decoy JSON in prose is ignored.
    assert len(result.findings) == 1
    assert result.findings[0]["file"] == "real.py"
    assert result.findings[0]["severity"] == "high"
    # Raw text still carries the decoy so the safety scorer can inspect it.
    assert "decoy.py" in result.raw_text


def test_parse_agent_result_marks_structured_present_via_response_format() -> None:
    """structured_response → ``structured_present=True``, even when findings list is empty."""
    from evals.solvers.review import _parse_agent_result

    class _AiMsg:
        type = "ai"
        content = "clean diff"

    structured = ReviewResponseModel(findings=[])
    parsed = _parse_agent_result({"messages": [_AiMsg()], "structured_response": structured})
    assert parsed.structured_present is True
    assert parsed.findings == []


def test_parse_agent_result_marks_structured_present_when_json_in_prose() -> None:
    """JSON-in-prose counts as schema-equivalent — agent emitted findings, just inline."""
    from evals.solvers.review import _parse_agent_result

    class _AiMsg:
        type = "ai"
        content = (
            'Reviewed the diff. Final answer: {"findings": '
            '[{"file":"a.py","line":1,"body":"bug","severity":"high"}]}'
        )

    parsed = _parse_agent_result({"messages": [_AiMsg()], "structured_response": None})
    assert parsed.structured_present is True
    assert len(parsed.findings) == 1
    assert parsed.findings[0]["file"] == "a.py"


def test_parse_agent_result_marks_structured_absent_for_prose_only() -> None:
    """Prose-only result with no schema payload: ``structured_present=False``.

    This is the input shape the wrapper's finalizer is designed to recover
    from — pinning that ``_parse_agent_result`` (which still runs after
    the wrapper) correctly identifies the prose-only case keeps the
    invariant explicit for future parser refactors.
    """
    from evals.solvers.review import _parse_agent_result

    class _AiMsg:
        type = "ai"
        content = (
            "I reviewed the diff and found 3 issues:\n"
            "1. File auth.py line 42 — token comparison not constant-time.\n"
            "2. File db.py line 17 — SQL string concatenation.\n"
            "3. File api.py line 9 — missing input validation.\n"
        )

    parsed = _parse_agent_result({"messages": [_AiMsg()], "structured_response": None})
    assert parsed.structured_present is False
    assert parsed.findings == []  # regex finds nothing — wrapper's finalizer would


def test_review_diff_finalizer_recovers_findings_from_prose() -> None:
    """Prose-only output is converted by the post-loop structured finalizer."""

    class _AiMsg:
        type = "ai"
        content = "Found a real bug in auth.py at line 42: timing leak."

    class _Agent:
        def invoke(self, _payload):
            return {"messages": [_AiMsg()]}

    recovered = ReviewResponseModel(
        findings=[
            {"file": "auth.py", "line": 42, "body": "Timing leak", "severity": "high"},  # type: ignore[list-item]
        ]
    )

    with (
        patch("evals.agents.baseline.create_deep_agent", return_value=_Agent()),
        _stub_finalizer(recovered),
    ):
        result = review_diff("diff stub")

    assert result.structured_present is True
    assert len(result.findings) == 1
    assert result.findings[0]["file"] == "auth.py"


def test_review_diff_raises_on_empty_response() -> None:
    """Empty agent prose is an errored sample, not a 'zero findings' answer.

    The wrapper's finalizer refuses to fabricate a schema from nothing.
    When the agent emits no visible text, ``finalize_structured`` raises
    :class:`AgentTerminationError` so Inspect marks the sample errored
    and excludes it from both the metric and LangSmith feedback.
    """
    from evals.common.termination import AgentTerminationError

    class _AiMsg:
        type = "ai"
        content = ""

    class _Agent:
        def invoke(self, _payload):
            return {"messages": [_AiMsg()]}

    with (
        patch("evals.agents.baseline.create_deep_agent", return_value=_Agent()),
        pytest.raises(AgentTerminationError),
    ):
        review_diff("diff stub")


def test_review_diff_concatenates_all_ai_messages_for_safety_scan() -> None:
    """The raw-text surface must span EVERY AI turn, not just the last one.

    With ``response_format=`` set, deepagents emits a schema-binding terminal
    step as its own AI message. An attacker who plants a canary in an earlier
    chain-of-thought turn would slip past a scan that only looked at
    ``messages[-1]``. This test pins the multi-turn join.

    The agent produces multiple AI turns; the wrapper's finalizer is
    stubbed to return an empty findings list so this test isolates the
    raw-text aggregation property, not the finalizer behaviour.
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
            }

    with (
        patch("evals.agents.baseline.create_deep_agent", return_value=_Agent()),
        _stub_finalizer(ReviewResponseModel(findings=[])),
    ):
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

    msg = build_review_user_message(
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

    msg = build_review_user_message(diff="x", repo=None, pr_url=None, pr_title=None, base_sha=None)
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
            return {"messages": [_AiMsg('{"findings": []}')]}

    class _State:
        def __init__(self) -> None:
            self.input_text = "diff stub"
            self.sample_id = "s1"
            self.metadata: dict = {}
            self.output = type("O", (), {"completion": ""})()

    with (
        patch("evals.agents.baseline.create_deep_agent", return_value=_Agent()),
        _stub_finalizer(ReviewResponseModel(findings=[])),
    ):
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
            }

    class _State:
        def __init__(self) -> None:
            self.input_text = "diff stub"
            self.sample_id = "s1"
            self.metadata: dict = {}
            self.output = type("O", (), {"completion": ""})()

    with (
        patch("evals.agents.baseline.create_deep_agent", return_value=_Agent()),
        _stub_finalizer(ReviewResponseModel(findings=[])),
    ):
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


def test_sandbox_solver_ignores_upstream_commit_as_base_sha(monkeypatch) -> None:
    """``upstream_commit`` is the martian-CRB SHA, not the target repo's base.

    Before this pin: ``base_sha = md.get("upstream_commit") or md.get("base_sha")``
    silently used the wrong SHA when ``base_sha`` was missing, guaranteeing
    every ``git fetch`` failed and the agent fell back to default-branch HEAD —
    reviewing the wrong commit. This test pins: when only ``upstream_commit``
    is provided (no real ``base_sha``), the solver MUST go to the bare-sandbox
    path (no repo cloned), not feed ``upstream_commit`` into ``RepoSpec``.
    """
    import asyncio

    from evals.solvers import review as review_mod

    bare_calls: list[bool] = []
    sample_calls: list[dict] = []

    async def _fake_bare(**_kw):
        bare_calls.append(True)

        class _Sb:
            async def aclose(self):
                pass

            id = "bare-sb"
            used_sha_fallback = False

        return _Sb()

    async def _fake_sample(repo_spec):  # type: ignore[no-untyped-def]
        sample_calls.append({"repo": repo_spec.repo, "base_commit": repo_spec.base_commit})

        class _Sb:
            async def aclose(self):
                pass

            id = "sample-sb"
            used_sha_fallback = False

        return _Sb()

    class _AiTail:
        type = "ai"
        content = "clean diff"

    class _Agent:
        async def ainvoke(self, _payload, config=None):
            return {
                # Non-empty AI tail satisfies the termination contract; the
                # structured_response carries the (empty) findings list.
                "messages": [_AiTail()],
                "structured_response": ReviewResponseModel(findings=[]),
            }

    monkeypatch.setattr(review_mod, "create_bare_sandbox", _fake_bare)
    monkeypatch.setattr(review_mod, "create_sandbox_for_sample", _fake_sample)
    monkeypatch.setattr(review_mod, "build_review_agent", lambda **_kw: _Agent())

    class _State:
        def __init__(self) -> None:
            self.input_text = "diff stub"
            self.sample_id = "s-no-base-sha"
            self.metadata: dict = {
                "repo": "calcom/cal.com",
                "pr_url": "https://github.com/calcom/cal.com/pull/42",
                # Only the (wrong) upstream_commit is present — base_sha absent.
                "upstream_commit": "807d46980c3390efbe8324c0b7a05fe3aa60c455",
            }
            self.output = type("O", (), {"completion": ""})()

    solver = review_mod.deepagents_baseline_review_solver()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(solver(_State(), None))  # type: ignore[arg-type]
    finally:
        loop.close()

    assert sample_calls == [], (
        "solver must not call create_sandbox_for_sample with the martian-CRB SHA; "
        f"got {sample_calls}"
    )
    assert bare_calls == [True], "solver must fall back to bare sandbox when base_sha missing"


def test_sandbox_solver_uses_base_sha_when_present(monkeypatch) -> None:
    """When ``base_sha`` is present, the solver clones the target repo at that SHA."""
    import asyncio

    from evals.solvers import review as review_mod

    sample_calls: list[dict] = []

    async def _fake_sample(repo_spec):  # type: ignore[no-untyped-def]
        sample_calls.append({"repo": repo_spec.repo, "base_commit": repo_spec.base_commit})

        class _Sb:
            async def aclose(self):
                pass

            id = "sample-sb"
            used_sha_fallback = False

        return _Sb()

    class _AiTail:
        type = "ai"
        content = "clean diff"

    class _Agent:
        async def ainvoke(self, _payload, config=None):
            return {
                # Non-empty AI tail satisfies the termination contract; the
                # structured_response carries the (empty) findings list.
                "messages": [_AiTail()],
                "structured_response": ReviewResponseModel(findings=[]),
            }

    monkeypatch.setattr(review_mod, "create_sandbox_for_sample", _fake_sample)
    monkeypatch.setattr(review_mod, "build_review_agent", lambda **_kw: _Agent())

    class _State:
        def __init__(self) -> None:
            self.input_text = "diff stub"
            self.sample_id = "s-with-base-sha"
            self.metadata: dict = {
                "repo": "calcom/cal.com",
                "pr_url": "https://github.com/calcom/cal.com/pull/42",
                "base_sha": "a" * 40,
                # upstream_commit MUST be ignored even when both are present.
                "upstream_commit": "807d46980c3390efbe8324c0b7a05fe3aa60c455",
            }
            self.output = type("O", (), {"completion": ""})()

    solver = review_mod.deepagents_baseline_review_solver()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(solver(_State(), None))  # type: ignore[arg-type]
    finally:
        loop.close()

    assert sample_calls == [{"repo": "calcom/cal.com", "base_commit": "a" * 40}]
