"""FakeLLM — in-memory LLMPort.

Records every complete() call as an immutable ``LLMCall`` frozen
dataclass. Inject responses via ``responses=[…]``; each call pops the
front item. Exhausting the list raises
``IndexError("FakeLLM: responses exhausted")``.

Failure injection: ``fail_after=N`` raises ``fail_with(…)`` once the
call count reaches ``N`` (sticky).

All-defaults constructor: ``FakeLLM()`` works with no args.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from openbot.application.ports.llm import LLMPort


@dataclass(frozen=True)
class LLMCall:
    """Snapshot of one complete() call. All fields immutable."""

    model: str
    messages: tuple[Any, ...]
    temperature: float
    max_tokens: int | None


@dataclass
class FakeLLM:
    """In-memory LLMPort. Construct fresh per test."""

    responses: list[str] = field(default_factory=list)
    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _calls: list[LLMCall] = field(default_factory=list, init=False)

    @property
    def calls(self) -> tuple[LLMCall, ...]:
        """All complete() calls in order, as an immutable tuple."""
        return tuple(self._calls)

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """See :class:`openbot.application.ports.llm.LLMPort`."""
        self._maybe_fail()
        self._calls.append(
            LLMCall(
                model=model,
                messages=tuple(messages),
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )
        if not self.responses:
            raise IndexError("FakeLLM: responses exhausted")
        return self.responses.pop(0)

    def _maybe_fail(self) -> None:
        if self.fail_after is not None and len(self._calls) >= self.fail_after:
            raise self.fail_with("FakeLLM: simulated failure")


# Static check: import-time error if FakeLLM stops satisfying LLMPort.
_PROTOCOL_CHECK: Final[LLMPort] = FakeLLM()


__all__ = ["FakeLLM", "LLMCall"]
