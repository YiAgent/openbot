from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openbot.domain.events import EventKind, UnifiedEvent


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


async def test_review_responder_builds_agent_with_review_model(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], *, config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            seen["payload"] = payload
            seen["config"] = config
            return {"messages": [SimpleNamespace(content="Reviewed: no blocking findings. LGTM.")]}

    def _fake_create_deep_agent(**kwargs: Any) -> _Agent:
        seen["kwargs"] = kwargs
        return _Agent()

    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", _fake_create_deep_agent)

    adapter = _StubAdapter("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n")
    reply = await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=adapter)

    assert reply == "Reviewed: no blocking findings. LGTM."
    assert adapter.calls == [("YiAgent/openbot", 42)]
    assert seen["kwargs"]["model"] == "anthropic:claude-opus-4-7"
    # Slice A2: agent now gets read_file + grep_repo tools.
    tool_names = {getattr(t, "name", None) for t in seen["kwargs"]["tools"]}
    assert tool_names == {"read_file", "grep_repo"}
    assert "senior code reviewer" in seen["kwargs"]["system_prompt"].lower()
    # Tool budget is enforced via recursion_limit on the ainvoke config.
    assert isinstance(seen["config"], dict)
    assert seen["config"].get("recursion_limit", 0) > 0
    prompt = seen["payload"]["messages"][0]["content"]
    assert "YiAgent/openbot" in prompt
    assert "#42" in prompt
    assert "diff --git" in prompt


async def test_review_responder_handles_empty_diff(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], *, config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            seen["payload"] = payload
            return {"messages": [SimpleNamespace(content="No diff available.")]}

    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _Agent())

    adapter = _StubAdapter("")  # closed / deleted PR
    reply = await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=adapter)

    assert reply == "No diff available."
    prompt = seen["payload"]["messages"][0]["content"]
    assert "(diff unavailable" in prompt


async def test_review_responder_truncates_huge_diffs(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], *, config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            seen["payload"] = payload
            return {"messages": [SimpleNamespace(content="OK.")]}

    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _Agent())

    huge = "x" * 2_000_000  # 2MB
    adapter = _StubAdapter(huge)
    await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=adapter)

    prompt = seen["payload"]["messages"][0]["content"]
    assert len(prompt) < 300_000  # well under any sane LLM context
    assert "(diff truncated" in prompt


async def test_review_responder_extracts_text_from_block_content(monkeypatch) -> None:
    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], *, config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {
                "messages": [
                    SimpleNamespace(
                        content=[
                            {"type": "text", "text": "First. "},
                            {"type": "text", "text": "Second."},
                        ]
                    )
                ]
            }

    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _Agent())

    reply = await mod.DeepAgentsReviewResponder().review_for_event(
        _event(), adapter=_StubAdapter("d")
    )
    assert reply == "First. Second."


async def test_review_responder_raises_on_empty_reply(monkeypatch) -> None:
    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], *, config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {"messages": [SimpleNamespace(content="   ")]}

    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _Agent())

    with pytest.raises(ValueError, match="deepagents_result_missing_text"):
        await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=_StubAdapter("d"))


async def test_review_responder_requires_pr_number(monkeypatch) -> None:
    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: object())

    with pytest.raises(ValueError, match="deepagents_review_requires_pr_number"):
        await mod.DeepAgentsReviewResponder().review_for_event(
            _event(pr_number=None), adapter=_StubAdapter("d")
        )


async def test_review_responder_rebuilds_agent_per_event(monkeypatch) -> None:
    """Tools close over (adapter, event) — caching by model alone is wrong.

    Without per-call rebuild, the second event would silently bind to the
    first event's adapter, which is a correctness bug (multi-tenant!).
    """
    builds: list[dict[str, Any]] = []

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], *, config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {"messages": [SimpleNamespace(content="ok")]}

    def _capture(**kwargs: Any) -> _Agent:
        builds.append(kwargs)
        return _Agent()

    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", _capture)

    responder = mod.DeepAgentsReviewResponder()
    await responder.review_for_event(_event(), adapter=_StubAdapter("a"))
    await responder.review_for_event(_event(), adapter=_StubAdapter("b"))

    # Two distinct builds — tools cannot leak between events.
    assert len(builds) == 2
    assert builds[0]["tools"] is not builds[1]["tools"]


async def test_review_responder_passes_recursion_limit(monkeypatch) -> None:
    """The recursion limit gates runaway tool loops at the langgraph layer.

    The ToolBudget guard catches the common case; recursion_limit catches
    pathological cases where the agent gets stuck in a non-tool loop.
    """
    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(
            self, payload: dict[str, Any], *, config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            seen["config"] = config
            return {"messages": [SimpleNamespace(content="ok")]}

    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _Agent())

    await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=_StubAdapter("d"))

    # Freeze the explicit limit so a future bump is intentional.
    assert seen["config"]["recursion_limit"] == 25
