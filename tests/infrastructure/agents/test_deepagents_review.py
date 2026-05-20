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
        async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
            seen["payload"] = payload
            return {"messages": [SimpleNamespace(content="Reviewed: no blocking findings. LGTM.")]}

    def _fake_create_deep_agent(**kwargs: Any) -> _Agent:
        seen["kwargs"] = kwargs
        return _Agent()

    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", _fake_create_deep_agent)
    mod._agent_for_model.cache_clear()

    adapter = _StubAdapter("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n")
    reply = await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=adapter)

    assert reply == "Reviewed: no blocking findings. LGTM."
    assert adapter.calls == [("YiAgent/openbot", 42)]
    assert seen["kwargs"]["model"] == "anthropic:claude-opus-4-7"
    assert seen["kwargs"]["tools"] == []
    assert "senior code reviewer" in seen["kwargs"]["system_prompt"].lower()
    prompt = seen["payload"]["messages"][0]["content"]
    assert "YiAgent/openbot" in prompt
    assert "#42" in prompt
    assert "diff --git" in prompt


async def test_review_responder_handles_empty_diff(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
            seen["payload"] = payload
            return {"messages": [SimpleNamespace(content="No diff available.")]}

    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _Agent())
    mod._agent_for_model.cache_clear()

    adapter = _StubAdapter("")  # closed / deleted PR
    reply = await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=adapter)

    assert reply == "No diff available."
    prompt = seen["payload"]["messages"][0]["content"]
    assert "(diff unavailable" in prompt


async def test_review_responder_truncates_huge_diffs(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
            seen["payload"] = payload
            return {"messages": [SimpleNamespace(content="OK.")]}

    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _Agent())
    mod._agent_for_model.cache_clear()

    huge = "x" * 2_000_000  # 2MB
    adapter = _StubAdapter(huge)
    await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=adapter)

    prompt = seen["payload"]["messages"][0]["content"]
    assert len(prompt) < 300_000  # well under any sane LLM context
    assert "(diff truncated" in prompt


async def test_review_responder_extracts_text_from_block_content(monkeypatch) -> None:
    class _Agent:
        async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
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
    mod._agent_for_model.cache_clear()

    reply = await mod.DeepAgentsReviewResponder().review_for_event(
        _event(), adapter=_StubAdapter("d")
    )
    assert reply == "First. Second."


async def test_review_responder_raises_on_empty_reply(monkeypatch) -> None:
    class _Agent:
        async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [SimpleNamespace(content="   ")]}

    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: _Agent())
    mod._agent_for_model.cache_clear()

    with pytest.raises(ValueError, match="deepagents_result_missing_text"):
        await mod.DeepAgentsReviewResponder().review_for_event(_event(), adapter=_StubAdapter("d"))


async def test_review_responder_requires_pr_number(monkeypatch) -> None:
    import openbot.infrastructure.agents.deepagents_review as mod

    monkeypatch.setattr(mod, "create_deep_agent", lambda **_: object())
    mod._agent_for_model.cache_clear()

    with pytest.raises(ValueError, match="deepagents_review_requires_pr_number"):
        await mod.DeepAgentsReviewResponder().review_for_event(
            _event(pr_number=None), adapter=_StubAdapter("d")
        )
