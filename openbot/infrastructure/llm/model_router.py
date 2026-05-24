"""LLM routing — feature → model name lookup.

PRD §13 #2 locked routing (final 1.0 decision, not configurable per request):

    triage / chat / review / fix  →  anthropic/glm-5.1   (beehears proxy)
    any feature fallback          →  openai/gpt-5-mini    (if Anthropic 5xx)

`LiteLLM` is the vendor abstraction; whatever model string we return is what
LiteLLM dispatches on. The `anthropic/` prefix tells LiteLLM to use the
Anthropic client path; ANTHROPIC_BASE_URL redirects those requests to the
beehears proxy so `glm-5.1` is the model actually served.

NOTE: deepseek-v4-pro / deepseek-v4-flash are reasoning models that require
`reasoning_content` to be echoed in multi-turn conversations. LangGraph does
not preserve that field in its message history, so those models fail with 400
on the second turn. GLM-5.1 (non-reasoning) is used instead.

Users can override per-feature primary in their `.openbot/config.yaml`
(PRD §6 `model.per_feature`) — this module exposes the **baked-in defaults**
only. Config overlay lands when YAML config loading does.

Why an enum and not free-form strings? PRD §4.5/§4.6 also key off feature
identity (per-task budget, rate limits, exempt roles). One symbol, one source
of truth.
"""

from __future__ import annotations

from typing import Final

# Re-exported from openbot.domain.workflows — kept for backward compat
from openbot.domain.workflows import Feature

__all__ = ["Feature", "fallback_model", "primary_model_for"]

# Frozen mapping — never mutate at runtime. Override via config overlay.
_PRIMARY: Final[dict[Feature, str]] = {
    Feature.TRIAGE: "anthropic/glm-5.1",
    Feature.CHAT: "anthropic/glm-5.1",
    Feature.REVIEW: "anthropic/glm-5.1",
    Feature.FIX: "anthropic/glm-5.1",
}

_FALLBACK: Final = "openai/gpt-5-mini"


def primary_model_for(feature: Feature) -> str:
    """Locked primary model for a given feature (PRD §13 #2)."""
    return _PRIMARY[feature]


def fallback_model() -> str:
    """Model used when the primary returns 5xx / rate-limits."""
    return _FALLBACK
