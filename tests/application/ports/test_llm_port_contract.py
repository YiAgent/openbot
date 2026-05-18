"""Contract test — FakeLLM records calls and returns programmed responses."""

from __future__ import annotations

import pytest

from tests._fakes.llm import FakeLLM


@pytest.mark.asyncio
async def test_complete_returns_default_response() -> None:
    llm = FakeLLM(response="hello")
    out = await llm.complete(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert out == "hello"
    assert len(llm.calls) == 1
    assert llm.calls[0]["model"] == "gpt-4o-mini"
    assert llm.calls[0]["temperature"] == 0.0
    assert llm.calls[0]["max_tokens"] is None


@pytest.mark.asyncio
async def test_queued_responses_drain_in_order() -> None:
    llm = FakeLLM(responses=["a", "b"])
    assert await llm.complete(model="m", messages=[]) == "a"
    assert await llm.complete(model="m", messages=[]) == "b"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_falls_back_to_default_after_queue_empty() -> None:
    llm = FakeLLM(response="default", responses=["first"])
    assert await llm.complete(model="m", messages=[]) == "first"
    assert await llm.complete(model="m", messages=[]) == "default"
