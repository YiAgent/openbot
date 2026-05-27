"""LLM routing — feature → model name lookup.

PRD §13 #2 locked routing (final 1.0 decision, not configurable per request):

    triage / chat / review / fix  →  OPENBOT_DEEPAGENTS_MODEL (default: anthropic/glm-5.1)
    any feature fallback          →  OPENBOT_DEEPAGENTS_FALLBACK_MODEL (default: openai/gpt-5-mini)

The **only** source of truth for model names is the environment:
``OPENBOT_DEEPAGENTS_MODEL`` and ``OPENBOT_DEEPAGENTS_FALLBACK_MODEL``,
read via :class:`openbot.core.settings.Settings`.  No model string is
hardcoded in this module — if the env vars are unset, the ``Settings``
field defaults apply.

``LiteLLM`` is the vendor abstraction; whatever model string we return is
what LiteLLM dispatches on. The ``anthropic/`` prefix tells LiteLLM to use
the Anthropic client path; ``ANTHROPIC_BASE_URL`` redirects those requests
to the configured proxy.

Users can override per-feature primary in their ``.openbot/config.yaml``
(PRD §6 ``model.per_feature``) — this module exposes the **env-driven
defaults** only. Config overlay lands when YAML config loading does.

Why an enum and not free-form strings? PRD §4.5/§4.6 also key off feature
identity (per-task budget, rate limits, exempt roles). One symbol, one source
of truth.
"""

from __future__ import annotations

# Re-exported from openbot.domain.workflows — kept for backward compat
from openbot.domain.workflows import Feature

__all__ = ["Feature", "fallback_model", "primary_model_for"]


def primary_model_for(feature: Feature) -> str:
    """Primary model for a given feature — reads from OPENBOT_DEEPAGENTS_MODEL.

    All features route to the same model today (PRD §13 #2). When per-feature
    overrides land via YAML config, this function becomes the merge point.
    """
    from openbot.core.settings import get_settings

    return get_settings().deepagents_model


def fallback_model() -> str:
    """Model used when the primary returns 5xx / rate-limits.

    Reads from OPENBOT_DEEPAGENTS_FALLBACK_MODEL.
    """
    from openbot.core.settings import get_settings

    return get_settings().deepagents_fallback_model
