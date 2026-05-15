"""LLM routing and completion helpers.

Imports are kept thin so `from openbot.llm import Feature` doesn't drag the
heavy LiteLLM module unless `complete()` is actually used.
"""

from openbot.llm.router import Feature, fallback_model, primary_model_for

__all__ = ["Feature", "fallback_model", "primary_model_for"]
