"""DeepAgentsFixResponder — wiring + schema-coercion tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.fix import FixAttempt, FixOutcome


async def test_fix_responder_delegates_to_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compatibility wrapper must delegate to BaseDeepAgentRuntime.run."""
    from openbot.domain.fix import FixOutcome
    from openbot.infrastructure.agents.deepagents_fix import DeepAgentsFixResponder, FixProfile
    from openbot.infrastructure.agents.runtime import BaseDeepAgentRuntime

    run_calls: list[Any] = []

    async def fake_run(self: Any, profile: Any, request: Any) -> FixOutcome:
        run_calls.append((profile, request))
        return FixOutcome(
            attempt=FixAttempt(
                summary="delegated",
                diff="",
                files_changed=(),
                tests_passed=True,
                test_command="pytest -q",
                test_output="",
            ),
        )

    monkeypatch.setattr(BaseDeepAgentRuntime, "run", fake_run)

    class _StubSandbox:
        pass

    class _StubAdapter:
        pass

    event = UnifiedEvent(
        channel="github",
        delivery_id="d-fix",
        kind=EventKind.ISSUE_ASSIGNED,
        repo="o/r",
        actor="alice",
        issue_number=42,
        installation_id=1,
    )
    result = await DeepAgentsFixResponder().fix_for_event(
        event,
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={"title": "Bug", "body": "It's broken", "base_sha": "abc123"},
    )

    assert len(run_calls) == 1
    assert isinstance(run_calls[0][0], FixProfile)
    assert result.attempt.summary == "delegated"


async def test_fix_profile_has_wall_seconds_limit() -> None:
    """FixProfile must declare a wall_seconds ceiling to cap runaway fix loops."""
    from openbot.infrastructure.agents.deepagents_fix import FixProfile

    profile = FixProfile()
    assert profile.limits.wall_seconds is not None
    assert profile.limits.wall_seconds >= 60


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
    import openbot.infrastructure.agents.runtime as runtime_mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            captured["payload"] = payload
            captured["config"] = config
            return _fake_agent_result()

    def fake_create_deep_agent(
        *,
        model: Any,
        tools: Any,
        system_prompt: Any,
        response_format: Any,
        middleware: Any = None,
        checkpointer: Any = None,
    ) -> FakeAgent:
        captured["model"] = model
        captured["tool_names"] = [t.name for t in tools]
        captured["response_format"] = response_format
        return FakeAgent()

    monkeypatch.setattr(runtime_mod, "create_deep_agent", fake_create_deep_agent)

    from openbot.infrastructure.agents import deepagents_fix as mod

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
    assert captured["config"]["recursion_limit"] == 60
    # Schema bridge wiring — pydantic class, not the dict shape.
    assert captured["response_format"].__name__ == "_FixOutcomeModel"


async def test_includes_issue_context_in_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    import openbot.infrastructure.agents.runtime as runtime_mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            captured["payload"] = payload
            return _fake_agent_result()

    def fake_create_deep_agent(**_: Any) -> FakeAgent:
        return FakeAgent()

    monkeypatch.setattr(runtime_mod, "create_deep_agent", fake_create_deep_agent)

    from openbot.infrastructure.agents import deepagents_fix as mod

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
    import openbot.infrastructure.agents.runtime as runtime_mod

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            return _fake_agent_result(
                tests_passed=False,
                test_output="1 failed, 2 passed",
            )

    monkeypatch.setattr(
        runtime_mod,
        "create_deep_agent",
        lambda **_: FakeAgent(),
    )

    from openbot.infrastructure.agents import deepagents_fix as mod

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
    import openbot.infrastructure.agents.runtime as runtime_mod
    from openbot.infrastructure.agents.profiles import AgentStructuredOutputError

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            return {"messages": []}  # no structured_response key

    monkeypatch.setattr(
        runtime_mod,
        "create_deep_agent",
        lambda **_: FakeAgent(),
    )

    from openbot.infrastructure.agents import deepagents_fix as mod

    responder = mod.DeepAgentsFixResponder()
    with pytest.raises(
        AgentStructuredOutputError,
        match="deepagents_fix_result_missing_structured_response",
    ):
        await responder.fix_for_event(
            _event(),
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            sandbox=_StubSandbox(),  # type: ignore[arg-type]
            issue={"title": "t", "body": "b", "base_sha": "abc1234"},
        )


async def test_raises_when_issue_number_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The responder cannot produce a useful prompt without an issue
    number — fail loudly rather than render ``#None`` and waste a model
    call. Mirrors ``test_review_responder_requires_pr_number``.
    """
    import openbot.infrastructure.agents.runtime as runtime_mod

    # The guard fires before ``make_fix_tools`` and ``create_deep_agent``,
    # so the monkeypatch returns an opaque sentinel — if the guard
    # regressed, the test would surface a different (less actionable)
    # failure from the stub, not a silent pass.
    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: object())

    from openbot.infrastructure.agents import deepagents_fix as mod

    event = UnifiedEvent(
        channel="github",
        delivery_id="d",
        kind=EventKind.ISSUE_ASSIGNED,
        repo="o/r",
        actor="alice",
        issue_number=None,
        installation_id=1,
    )

    responder = mod.DeepAgentsFixResponder()
    with pytest.raises(ValueError, match="deepagents_fix_requires_issue_number"):
        await responder.fix_for_event(
            event,
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            sandbox=_StubSandbox(),  # type: ignore[arg-type]
            issue={"title": "t", "body": "b", "base_sha": "abc1234"},
        )


async def test_fix_responder_passes_checkpointer_and_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """checkpointer is forwarded to create_deep_agent; run_id becomes
    thread_id in config["configurable"] when both are provided.
    """
    from langgraph.checkpoint.memory import MemorySaver

    import openbot.infrastructure.agents.runtime as runtime_mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            captured["config"] = config
            return _fake_agent_result()

    def fake_create_deep_agent(
        *,
        model: Any,
        tools: Any,
        system_prompt: Any,
        response_format: Any,
        middleware: Any = None,
        checkpointer: Any = None,
    ) -> FakeAgent:
        captured["checkpointer"] = checkpointer
        return FakeAgent()

    monkeypatch.setattr(runtime_mod, "create_deep_agent", fake_create_deep_agent)

    from openbot.infrastructure.agents import deepagents_fix as mod

    saver = MemorySaver()
    responder = mod.DeepAgentsFixResponder()
    await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={"title": "t", "body": "b", "base_sha": "abc1234"},
        run_id="run-abc",
        checkpointer=saver,
    )

    assert captured["checkpointer"] is saver
    assert captured["config"]["configurable"]["thread_id"] == "run-abc"


async def test_fix_responder_no_checkpointer_no_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When run_id / checkpointer are omitted, checkpointer=None is passed
    to create_deep_agent and no 'configurable' key is added to config.
    """
    import openbot.infrastructure.agents.runtime as runtime_mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            captured["config"] = config
            return _fake_agent_result()

    def fake_create_deep_agent(
        *,
        model: Any,
        tools: Any,
        system_prompt: Any,
        response_format: Any,
        middleware: Any = None,
        checkpointer: Any = None,
    ) -> FakeAgent:
        captured["checkpointer"] = checkpointer
        return FakeAgent()

    monkeypatch.setattr(runtime_mod, "create_deep_agent", fake_create_deep_agent)

    from openbot.infrastructure.agents import deepagents_fix as mod

    responder = mod.DeepAgentsFixResponder()
    await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={"title": "t", "body": "b", "base_sha": "abc1234"},
        # no run_id, no checkpointer
    )

    assert captured["checkpointer"] is None
    assert "configurable" not in captured["config"]


async def test_responder_rebuilds_agent_per_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tools close over ``(sandbox, event)`` — caching by model alone
    would leak a previous tenant's sandbox handle into the next event.
    Two consecutive calls must produce distinct ``tools`` lists.
    Mirrors ``test_review_responder_rebuilds_agent_per_event``.
    """
    import openbot.infrastructure.agents.runtime as runtime_mod

    builds: list[dict[str, Any]] = []

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
            return _fake_agent_result()

    def _capture(**kwargs: Any) -> FakeAgent:
        builds.append(kwargs)
        return FakeAgent()

    monkeypatch.setattr(runtime_mod, "create_deep_agent", _capture)

    from openbot.infrastructure.agents import deepagents_fix as mod

    responder = mod.DeepAgentsFixResponder()
    await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={"title": "t", "body": "b", "base_sha": "abc1234"},
    )
    await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={"title": "t", "body": "b", "base_sha": "abc1234"},
    )

    # Two distinct builds — tools cannot leak between events.
    assert len(builds) == 2
    assert builds[0]["tools"] is not builds[1]["tools"]
