"""LLM routing — feature → model name lookup.

PRD §13 #2 locked routing (final 1.0 decision, not configurable per request):

    triage / chat       →  anthropic/claude-sonnet-4-6      (cheap & fast)
    review / fix        →  anthropic/claude-opus-4-7        (high stakes)
    any feature fallback→  openai/gpt-5-mini                (if Anthropic 5xx)

`LiteLLM` is the vendor abstraction; whatever model string we return is what
LiteLLM dispatches on. Users can override per-feature primary in their
`.openbot/config.yaml` (PRD §6 `model.per_feature`) — this module exposes the
**baked-in defaults** only. Config overlay lands when YAML config loading does.

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
    Feature.TRIAGE: "anthropic/claude-sonnet-4-6",
    Feature.CHAT: "anthropic/claude-sonnet-4-6",
    Feature.REVIEW: "anthropic/claude-opus-4-7",
    Feature.FIX: "anthropic/claude-opus-4-7",
}

_FALLBACK: Final = "openai/gpt-5-mini"


def primary_model_for(feature: Feature) -> str:
    """Locked primary model for a given feature (PRD §13 #2)."""
    return _PRIMARY[feature]


def fallback_model() -> str:
    """Model used when the primary returns 5xx / rate-limits."""
    return _FALLBACK
