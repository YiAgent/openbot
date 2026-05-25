"""Contract tests for LLMPort.

[fake] uses FakeLLM (response-queue), [real] uses LiteLLMCompleter
with litellm.acompletion monkeypatched so no real API call is made.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest

from openbot.application.ports.llm import LLMPort
from openbot.infrastructure.llm.complete import LiteLLMCompleter
from openbot.testing.fakes import FakeLLM

try:
    import litellm as _litellm
except ImportError:
    _litellm = None  # type: ignore[assignment]


class _FakeChoice:
    class message:
        content = "mocked response"


class _FakeResponse:
    choices: ClassVar[list[_FakeChoice]] = [_FakeChoice()]


@pytest.fixture(params=["fake", "real"])
async def llm(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LLMPort]:
    if request.param == "fake":
        yield FakeLLM(responses=["mocked response", "mocked response", "mocked response"])
    else:
        if _litellm is None:
            pytest.skip("litellm not installed")
        monkeypatch.setattr(_litellm, "acompletion", AsyncMock(return_value=_FakeResponse()))
        yield LiteLLMCompleter()


@pytest.mark.contract
class TestLLMContract:
    async def test_complete_returns_str(self, llm: LLMPort) -> None:
        result = await llm.complete(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "Say hello."}],
        )
        assert isinstance(result, str)

    async def test_complete_returns_nonempty_string(self, llm: LLMPort) -> None:
        result = await llm.complete(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "Hello."}],
        )
        assert len(result) > 0

    async def test_temperature_argument_accepted(self, llm: LLMPort) -> None:
        result = await llm.complete(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "Hello."}],
            temperature=0.0,
        )
        assert isinstance(result, str)

    async def test_max_tokens_argument_accepted(self, llm: LLMPort) -> None:
        result = await llm.complete(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "Hello."}],
            max_tokens=8,
        )
        assert isinstance(result, str)

    async def test_fake_exhausted_raises_index_error(self) -> None:
        llm = FakeLLM()  # no responses
        with pytest.raises(IndexError):
            await llm.complete(
                model="claude-haiku-4-5",
                messages=[{"role": "user", "content": "hello"}],
            )
