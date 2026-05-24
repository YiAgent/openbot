# openbot/infrastructure/agents/model_names.py
"""Model name helpers for the DeepAgents runtime.

Two distinct transforms:
  normalize_for_langchain — LiteLLM "provider/name" → LangChain "provider:name"
  display_name            — strip "provider:" prefix for logs and LangSmith traces

They are kept separate because they serve different callers with different
intent: routing vs. human-readable display.
"""

from __future__ import annotations


def normalize_for_langchain(model: str) -> str:
    """Map provider/name (LiteLLM) → provider:name (langchain).

    anthropic/GLM-5.1 → anthropic:GLM-5.1
    anthropic:GLM-5.1 → anthropic:GLM-5.1  (idempotent)
    GLM-5.1           → GLM-5.1            (bare names untouched)
    """
    if ":" in model:
        return model
    if "/" in model:
        provider, name = model.split("/", 1)
        return f"{provider}:{name}"
    return model


def display_name(model: str) -> str:
    """Strip the provider: prefix for human-facing surfaces (logs, LangSmith).

    anthropic:GLM-5.1 → GLM-5.1
    GLM-5.1           → GLM-5.1        (idempotent)
    openai:org:model  → org:model      (only the first segment is stripped)
    """
    if ":" not in model:
        return model
    return model.split(":", 1)[1]


__all__ = ["display_name", "normalize_for_langchain"]
