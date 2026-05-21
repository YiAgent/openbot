"""DeepAgentsFixResponder — wiring + schema-coercion tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.fix import FixAttempt, FixOutcome


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d",
        kind=EventKind.ISSUE_ASSIGNED,
        repo="o/r",
        actor="alice",
        issue_number=7,
        installation_id=1,
    )


@dataclass
class _StubAdapter:
    """Adapter is unused by the responder body (the use case handles
    GitHub I/O), but the responder signature accepts one for symmetry
    with ``DeepAgentsReviewResponder``."""


@dataclass
class _StubSandbox:
    """Sandbox is passed through to ``make_fix_tools``; the agent stub
    we monkeypatch never invokes the tools, so this can stay empty."""


def _fake_agent_result(
    *,
    summary: str = "fix off-by-one",
    tests_passed: bool = True,
    test_output: str = "3 passed",
    files_changed: tuple[str, ...] = ("src/api/list.py",),
) -> dict[str, Any]:
    """Shape returned by ``create_deep_agent(...).ainvoke(...)`` when
    ``response_format=FixOutcomeSchema`` is set."""

    return {
        "messages": [],
        "structured_response": {
            "attempt": {
                "summary": summary,
                "files_changed": list(files_changed),
                "tests_passed": tests_passed,
                "test_command": "pytest -q",
                "test_output": test_output,
                "diff": "diff --git a/x b/x\n",
            },
        },
    }


async def test_returns_fix_outcome_when_agent_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from openbot.infrastructure.agents import deepagents_fix as mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            captured["payload"] = payload
            captured["config"] = config
            return _fake_agent_result()

    def fake_create_deep_agent(
        *, model: Any, tools: Any, system_prompt: Any, response_format: Any
    ) -> FakeAgent:
        captured["model"] = model
        captured["tool_names"] = [t.name for t in tools]
        captured["response_format"] = response_format
        return FakeAgent()

    monkeypatch.setattr(mod, "create_deep_agent", fake_create_deep_agent)

    responder = mod.DeepAgentsFixResponder()
    outcome = await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={
            "title": "Off-by-one on pagination",
            "body": "Last item is dropped when total % page_size == 0.",
            "base_sha": "abc1234",
        },
    )

    assert isinstance(outcome, FixOutcome)
    assert isinstance(outcome.attempt, FixAttempt)
    assert outcome.attempt.tests_passed is True
    assert outcome.attempt.files_changed == ("src/api/list.py",)
    # The schema bridge translates lists → tuples (frozen invariants).

    # Wiring: tool names match the C.6 contract; recursion limit honoured.
    assert captured["tool_names"] == [
        "read_file",
        "write_file",
        "list_files",
        "run_command",
        "git_diff",
        "search_files",
    ]
    assert captured["config"]["recursion_limit"] == 25
    # Schema bridge wiring — pydantic class, not the dict shape.
    assert captured["response_format"].__name__ == "_FixOutcomeModel"


async def test_includes_issue_context_in_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    from openbot.infrastructure.agents import deepagents_fix as mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            captured["payload"] = payload
            return _fake_agent_result()

    def fake_create_deep_agent(**_: Any) -> FakeAgent:
        return FakeAgent()

    monkeypatch.setattr(mod, "create_deep_agent", fake_create_deep_agent)

    responder = mod.DeepAgentsFixResponder()
    await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={
            "title": "Off-by-one on pagination",
            "body": "Last item is dropped when total % page_size == 0.",
            "base_sha": "abc1234",
        },
    )

    user_msg = captured["payload"]["messages"][0]["content"]
    assert "Off-by-one on pagination" in user_msg
    assert "page_size == 0" in user_msg
    assert "o/r" in user_msg
    assert "#7" in user_msg
    # The agent should know the base commit so it can ground hashes in
    # tool calls if needed.
    assert "abc1234" in user_msg


async def test_returns_failure_outcome_when_tests_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from openbot.infrastructure.agents import deepagents_fix as mod

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            return _fake_agent_result(
                tests_passed=False,
                test_output="1 failed, 2 passed",
            )

    monkeypatch.setattr(
        mod,
        "create_deep_agent",
        lambda **_: FakeAgent(),
    )

    responder = mod.DeepAgentsFixResponder()
    outcome = await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={"title": "t", "body": "b", "base_sha": "abc1234"},
    )

    # Tests-failed is a *legitimate* terminal outcome — the responder
    # just reports the attempt; the use case decides comment vs PR.
    assert outcome.attempt.tests_passed is False
    assert outcome.pr_url is None
    assert outcome.error is None
    assert "1 failed" in outcome.attempt.test_output


async def test_raises_when_structured_response_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from openbot.infrastructure.agents import deepagents_fix as mod

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            return {"messages": []}  # no structured_response key

    monkeypatch.setattr(
        mod,
        "create_deep_agent",
        lambda **_: FakeAgent(),
    )

    responder = mod.DeepAgentsFixResponder()
    with pytest.raises(ValueError, match="deepagents_fix_result_missing_structured_response"):
        await responder.fix_for_event(
            _event(),
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            sandbox=_StubSandbox(),  # type: ignore[arg-type]
            issue={"title": "t", "body": "b", "base_sha": "abc1234"},
        )
