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
        """Extract token usage from each LLM call's output.

        Token usage can appear in several places depending on the provider:
          1. ``msg.usage_metadata`` — langchain-anthropic / langchain-openai
          2. ``msg.response_metadata["usage"]`` — some providers (proxies)
          3. ``llm_output["token_usage"]`` — older LangChain convention
          4. ``llm_output["usage"]`` — some providers (e.g. Xiaomi proxy)
        We try all sources but only count each once per call.
        """
        call_input = 0
        call_output = 0
        call_total = 0

        for generation_list in response.generations:
            for gen in generation_list:
                msg = getattr(gen, "message", None)
                if msg is None:
                    continue
                # Source 1: usage_metadata (langchain-anthropic, langchain-openai)
                usage = getattr(msg, "usage_metadata", None)
                if usage and isinstance(usage, dict):
                    call_input = max(call_input, usage.get("input_tokens", 0) or 0)
                    call_output = max(call_output, usage.get("output_tokens", 0) or 0)
                    call_total = max(call_total, usage.get("total_tokens", 0) or 0)
                    continue  # primary source found, skip fallbacks for this msg

                # Source 2: response_metadata["usage"] (proxy models)
                resp_meta = getattr(msg, "response_metadata", None) or {}
                resp_usage = resp_meta.get("usage")
                if resp_usage and isinstance(resp_usage, dict):
                    call_input = max(
                        call_input,
                        resp_usage.get("input_tokens", 0)
                        or resp_usage.get("prompt_tokens", 0)
                        or 0,
                    )
                    call_output = max(
                        call_output,
                        resp_usage.get("output_tokens", 0)
                        or resp_usage.get("completion_tokens", 0)
                        or 0,
                    )
                    call_total = max(
                        call_total,
                        resp_usage.get("total_tokens", 0) or 0,
                    )

        # Source 3 & 4: llm_output (older conventions)
        llm_output = response.llm_output or {}
        if not call_input:
            for key in ("token_usage", "usage"):
                llm_usage = llm_output.get(key, {})
                if llm_usage and isinstance(llm_usage, dict):
                    call_input = max(call_input, llm_usage.get("prompt_tokens", 0) or 0)
                    call_output = max(call_output, llm_usage.get("completion_tokens", 0) or 0)
                    call_total = max(call_total, llm_usage.get("total_tokens", 0) or 0)
                    break

        self.input_tokens += call_input
        self.output_tokens += call_output
        self.total_tokens += call_total

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
