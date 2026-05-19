"""LLMPort — single-call completion contract.

Defined for the agent slice; no Phase-2 consumer wires through it yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class LLMPort(Protocol):
    """One-shot chat completion.

    Returns the assistant text only. Callers that need cost accounting
    should use ``openbot.infrastructure.llm.complete`` directly; this Port
    is the minimal interface for agent tool loops where cost is tracked
    externally.
    """

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """Return the assistant message content. Raises on transport error."""
        ...


__all__ = ["LLMPort"]
