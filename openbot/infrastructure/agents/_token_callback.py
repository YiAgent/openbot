"""LangChain callback that captures per-invocation token usage.

Injected alongside the Langfuse handler in :meth:`BaseDeepAgentRuntime.run`.
After the agent completes, callers read :attr:`usage` to get the aggregated
token counts and feed them into Inspect AI's ``state.metadata["model_usage"]``
so the eval pipeline has full token visibility.

Why a callback and not post-hoc extraction:
  LangChain's ``AIMessage.usage_metadata`` is only populated by some providers
  (OpenAI, Anthropic via ``langchain-anthropic``).  The callback approach works
  universally — ``on_llm_end`` receives the ``LLMOutput`` dict which every
  provider fills.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

_logger = logging.getLogger(__name__)


class TokenUsageCallback(BaseCallbackHandler):
    """Accumulate token usage across all LLM calls in one agent invocation."""

    def __init__(self) -> None:
        super().__init__()
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.total_tokens: int = 0
        self.total_cost: float = 0.0
        self.model_name: str = "unknown"

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Extract token usage from each LLM call's output."""
        for generation_list in response.generations:
            for gen in generation_list:
                msg = getattr(gen, "message", None)
                if msg is None:
                    continue
                # langchain-anthropic and langchain-openai both populate this.
                usage = getattr(msg, "usage_metadata", None)
                if usage is not None:
                    self.input_tokens += getattr(usage, "input_tokens", 0) or 0
                    self.output_tokens += getattr(usage, "output_tokens", 0) or 0
                    self.total_tokens += getattr(usage, "total_tokens", 0) or 0

        # Also check llm_output for providers that put usage there.
        llm_output = response.llm_output or {}
        token_usage = llm_output.get("token_usage", {})
        if token_usage and not self.input_tokens:
            self.input_tokens += token_usage.get("prompt_tokens", 0)
            self.output_tokens += token_usage.get("completion_tokens", 0)
            self.total_tokens += token_usage.get("total_tokens", 0)

        # Track model name for labeling.
        model_name = llm_output.get("model_name") or llm_output.get("model")
        if model_name:
            self.model_name = str(model_name)

    @property
    def usage(self) -> dict[str, Any]:
        """Return usage in a format compatible with Inspect AI's ModelUsage."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "model_name": self.model_name,
        }


__all__ = ["TokenUsageCallback"]
