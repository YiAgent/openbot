from __future__ import annotations

from typing import Any

import pytest

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.review import Finding, ReviewFindings
from openbot.infrastructure.agents._review_schema import (
    FindingSchema,
    ReviewFindingsSchema,
)


def _event(*, repo: str = "YiAgent/openbot", pr_number: int | None = 42) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="review-deliv-1",
        kind=EventKind.PR_OPENED,
        repo=repo,
        actor="alice",
        pr_number=pr_number,
        installation_id=101,
    )


class _StubAdapter:
    """Minimal ChannelAdapterPort stand-in: only ``get_pr_diff`` is exercised."""

    def __init__(self, diff: str) -> None:
        self._diff = diff
        self.calls: list[tuple[str, int]] = []

    async def get_pr_diff(self, event: UnifiedEvent, pr_number: int) -> str:
        self.calls.append((event.repo, pr_number))
        return self._diff


def _findings_payload(*findings: FindingSchema, summary: str = "ok") -> ReviewFindingsSchema:
    return ReviewFindingsSchema(summary=summary, findings=list(findings))


async def test_review_responder_builds_agent_with_review_model(monkeypatch) -> None:
    import openbot.infrastructure.agents.runtime as runtime_mod

    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            seen["payload"] = payload
            seen["config"] = config
            return {
                "structured_response": _findings_payload(summary="Reviewed: no blocking findings.")
            }

    def _fake_create_deep_agent(**kwargs: Any) -> _Agent:
        seen["kwargs"] = kwargs
        return _Agent()

    monkeypatch.setattr(runtime_mod, "create_deep_agent", _fake_create_deep_agent)

    import openbot.infrastructure.agents.deepagents_review as mod

    adapter = _StubAdapter("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n")
    result = await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=adapter)  # type: ignore[arg-type]

    assert isinstance(result, ReviewFindings)
    assert result.summary == "Reviewed: no blocking findings."
    assert result.findings == ()
    assert adapter.calls == [("YiAgent/openbot", 42)]
    # Slice A2: agent gets read_file + grep_repo tools.
    tool_names = {getattr(t, "name", None) for t in seen["kwargs"]["tools"]}
    assert tool_names == {"read_file", "grep_repo"}
    assert "senior code reviewer" in seen["kwargs"]["system_prompt"].lower()
    # Slice B: structured output is required end-to-end.
    assert seen["kwargs"]["response_format"] is ReviewFindingsSchema
    # Tool budget is enforced via recursion_limit on the ainvoke config.
    assert isinstance(seen["config"], dict)
    assert seen["config"].get("recursion_limit", 0) > 0
    prompt = seen["payload"]["messages"][0]["content"]
    assert "YiAgent/openbot" in prompt
    assert "#42" in prompt
    assert "diff --git" in prompt


async def test_review_responder_returns_findings_from_structured_response(monkeypatch) -> None:
    """The agent's structured findings round-trip into domain ReviewFindings."""
    import openbot.infrastructure.agents.runtime as runtime_mod

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {
                "structured_response": _findings_payload(
                    FindingSchema(severity="high", file="src/x.py", message="null deref", line=10),
                    FindingSchema(severity="nit", file="src/x.py", message="style"),
                    summary="2 findings",
                )
            }

    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: _Agent())

    import openbot.infrastructure.agents.deepagents_review as mod

    result = await mod.DeepAgentsReviewResponder().review_for_event(
        _event(),
        adapter=_StubAdapter("d"),  # type: ignore[arg-type]
    )

    assert result == ReviewFindings(
        summary="2 findings",
        findings=(
            Finding(severity="high", file="src/x.py", message="null deref", line=10),
            Finding(severity="nit", file="src/x.py", message="style"),
        ),
    )


async def test_review_responder_handles_empty_diff(monkeypatch) -> None:
    import openbot.infrastructure.agents.runtime as runtime_mod

    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            seen["payload"] = payload
            return {"structured_response": _findings_payload(summary="No diff available.")}

    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: _Agent())

    import openbot.infrastructure.agents.deepagents_review as mod

    adapter = _StubAdapter("")  # closed / deleted PR
    result = await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=adapter)  # type: ignore[arg-type]

    assert result.summary == "No diff available."
    prompt = seen["payload"]["messages"][0]["content"]
    assert "(diff unavailable" in prompt


async def test_review_responder_truncates_huge_diffs(monkeypatch) -> None:
    import openbot.infrastructure.agents.runtime as runtime_mod

    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            seen["payload"] = payload
            return {"structured_response": _findings_payload(summary="OK.")}

    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: _Agent())

    import openbot.infrastructure.agents.deepagents_review as mod

    huge = "x" * 2_000_000  # 2MB
    adapter = _StubAdapter(huge)
    await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=adapter)  # type: ignore[arg-type]

    prompt = seen["payload"]["messages"][0]["content"]
    assert len(prompt) < 300_000  # well under any sane LLM context
    assert "(diff truncated" in prompt


async def test_review_responder_raises_on_missing_structured_response(monkeypatch) -> None:
    """No structured_response → fail loud; the use case posts the error template."""
    import openbot.infrastructure.agents.runtime as runtime_mod
    from openbot.infrastructure.agents.profiles import AgentStructuredOutputError

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {"messages": []}  # no structured_response key at all

    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: _Agent())

    import openbot.infrastructure.agents.deepagents_review as mod

    # AgentStructuredOutputError is a subclass of RuntimeError; the old test matched ValueError
    # because the responder raised it directly. Now parse_result raises AgentStructuredOutputError.
    with pytest.raises(
        AgentStructuredOutputError, match="deepagents_result_missing_structured_response"
    ):
        await mod.DeepAgentsReviewResponder().review_for_event(
            _event(),
            adapter=_StubAdapter("d"),  # type: ignore[arg-type]
        )


async def test_review_responder_requires_pr_number(monkeypatch) -> None:
    import openbot.infrastructure.agents.deepagents_review as mod

    with pytest.raises(ValueError, match="deepagents_review_requires_pr_number"):
        await mod.DeepAgentsReviewResponder().review_for_event(
            _event(pr_number=None),
            adapter=_StubAdapter("d"),  # type: ignore[arg-type]
        )


async def test_review_responder_rebuilds_agent_per_event(monkeypatch) -> None:
    """Tools close over (adapter, event) — caching by model alone is wrong."""
    import openbot.infrastructure.agents.runtime as runtime_mod

    builds: list[dict[str, Any]] = []

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {"structured_response": _findings_payload(summary="ok")}

    def _capture(**kwargs: Any) -> _Agent:
        builds.append(kwargs)
        return _Agent()

    monkeypatch.setattr(runtime_mod, "create_deep_agent", _capture)

    import openbot.infrastructure.agents.deepagents_review as mod

    responder = mod.DeepAgentsReviewResponder()
    await responder.review_for_event(_event(), adapter=_StubAdapter("a"))  # type: ignore[arg-type]
    await responder.review_for_event(_event(), adapter=_StubAdapter("b"))  # type: ignore[arg-type]

    # Two distinct builds — tools cannot leak between events.
    assert len(builds) == 2
    assert builds[0]["tools"] is not builds[1]["tools"]


async def test_review_responder_passes_recursion_limit(monkeypatch) -> None:
    """The recursion limit gates runaway tool loops at the langgraph layer."""
    import openbot.infrastructure.agents.runtime as runtime_mod

    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            seen["config"] = config
            return {"structured_response": _findings_payload(summary="ok")}

    monkeypatch.setattr(runtime_mod, "create_deep_agent", lambda **_: _Agent())

    import openbot.infrastructure.agents.deepagents_review as mod

    await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=_StubAdapter("d"))  # type: ignore[arg-type]

    # Freeze the explicit limit so a future bump is intentional.
    assert seen["config"]["recursion_limit"] == 25


async def test_review_responder_passes_checkpointer_and_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    import openbot.infrastructure.agents.runtime as runtime_mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any = None) -> dict[str, Any]:
            captured["config"] = config
            return {"structured_response": _findings_payload(summary="ok")}

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

    from openbot.infrastructure.agents import deepagents_review as mod

    saver = MemorySaver()
    responder = mod.DeepAgentsReviewResponder()
    await responder.review_for_event(
        _event(),
        adapter=_StubAdapter("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new"),  # type: ignore[arg-type]
        run_id="run-review-1",
        checkpointer=saver,
    )

    assert captured["checkpointer"] is saver
    assert captured["config"]["configurable"]["thread_id"] == "run-review-1"


async def test_review_responder_no_checkpointer_no_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbot.infrastructure.agents.runtime as runtime_mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: Any, config: Any = None) -> dict[str, Any]:
            captured["config"] = config
            return {"structured_response": _findings_payload(summary="ok")}

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

    from openbot.infrastructure.agents import deepagents_review as mod

    responder = mod.DeepAgentsReviewResponder()
    await responder.review_for_event(
        _event(),
        adapter=_StubAdapter("d"),  # type: ignore[arg-type]
        # run_id and checkpointer intentionally omitted
    )

    assert captured["checkpointer"] is None
    assert "configurable" not in captured["config"]


async def test_review_responder_delegates_to_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compatibility wrapper must delegate to BaseDeepAgentRuntime.run."""
    from openbot.infrastructure.agents.deepagents_review import (
        DeepAgentsReviewResponder,
        ReviewProfile,
    )
    from openbot.infrastructure.agents.runtime import BaseDeepAgentRuntime

    run_calls: list[Any] = []

    async def fake_run(self: Any, profile: Any, request: Any) -> ReviewFindings:
        run_calls.append((profile, request))
        return ReviewFindings(summary="delegated", findings=())

    monkeypatch.setattr(BaseDeepAgentRuntime, "run", fake_run)

    class _StubAdapter:
        async def get_pr_diff(self, event: Any, pr_number: Any) -> str:
            return "--- a/x\n+++ b/x"

    event = UnifiedEvent(
        channel="github",
        delivery_id="d-1",
        kind=EventKind.PR_OPENED,
        repo="o/r",
        actor="alice",
        pr_number=1,
        installation_id=1,
    )
    result = await DeepAgentsReviewResponder().review_for_event(event, adapter=_StubAdapter())  # type: ignore[arg-type]

    assert len(run_calls) == 1
    assert isinstance(run_calls[0][0], ReviewProfile)
    assert result.summary == "delegated"
