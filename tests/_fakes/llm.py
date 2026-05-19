"""FakeLLM — programmable LLMPort that records every call."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeLLM:
    """Single-response or queued-response fake LLM.

    ``responses`` drains in order; falls back to ``response`` once empty.
    """

    response: str = ""
    responses: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        self.calls.append(
            {
                "model": model,
                "messages": list(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        return self.response


__all__ = ["FakeLLM"]
