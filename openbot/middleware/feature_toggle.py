"""FeatureToggle middleware — `.openbot/config.yaml` `features.{x}=false` enforcement.

Spec anchor: docs/_archive/prd-history/openbot-harness-spec.md §3 M1
(the ``features`` block in ``EffectiveConfig`` is loaded but slice A/B
never wired it into a gate; this closes that commitment). The harness
spec is archived; current chain layout lives in
docs/specs/2026-05-17-webhook-worker-layering-design.md §3.2 D4.

Position in chain: directly after ``KillSwitchMiddleware``.

  - Cheaper than ``CancelLabelMiddleware`` (no GitHub API call).
  - More expensive than ``KillSwitchMiddleware`` (one config read
    vs. one env-var lookup), which is why the env kill switch
    still sits in front.

Decision contract:

  - ``features.{feature}=False``     → BLOCKED, ``reason="feature_disabled:<feature>"``,
                                       audit phase ``SKIPPED``, **no reply comment**.
                                       The user disabled this themselves; an
                                       "OpenBot ignored you" comment would
                                       be noise.
  - ``features.{feature}=True`` (default) → PROCEED.

Audit phase rationale: this is a soft / user-configured gate, not a
hard rejection like ``KillSwitch`` or ``ActorRole``. ``SKIPPED`` keeps
the audit row distinguishable from rejections in dashboards.
"""

from __future__ import annotations

import logging

from openbot.llm.router import Feature
from openbot.middleware.preflight import (
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
)
from openbot.persistence.models import WorkflowPhase

_logger = logging.getLogger(__name__)


# Map Feature → attribute on ``EffectiveConfig.features``. A dict
# (not getattr-by-name) so an accidental enum addition without a
# matching toggle field is a KeyError at chain build time, not a
# silently-True bypass at request time.
_FEATURE_TO_ATTR: dict[Feature, str] = {
    Feature.TRIAGE: "triage",
    Feature.REVIEW: "review",
    Feature.FIX: "fix",
    Feature.CHAT: "chat",
}


class FeatureToggleMiddleware:
    """Block when ``EffectiveConfig.features.{feature}`` is False."""

    name = "feature_toggle"

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        feature = ctx.dispatch.feature
        attr = _FEATURE_TO_ATTR.get(feature)
        if attr is None:
            # Defensive: a new Feature enum value without a toggle
            # mapping would silently bypass this gate. Surface it.
            _logger.warning(
                "feature_toggle_unknown_feature",
                extra={
                    "delivery_id": ctx.event.delivery_id,
                    "repo": ctx.event.repo,
                    "feature": feature.value,
                },
            )
            return MiddlewareDecision.proceed()

        enabled = bool(getattr(ctx.config.features, attr))
        if enabled:
            return MiddlewareDecision.proceed()

        _logger.info(
            "feature_toggle_blocked",
            extra={
                "delivery_id": ctx.event.delivery_id,
                "repo": ctx.event.repo,
                "feature": feature.value,
            },
        )
        return MiddlewareDecision(
            result=MiddlewareResult.BLOCKED,
            reason=f"feature_disabled:{feature.value}",
            audit_phase=WorkflowPhase.SKIPPED,
            # No comment — the user explicitly disabled this in their
            # config; informing them every time is noise. Audit row
            # is enough for "why didn't OpenBot react?" debugging.
        )


__all__ = ["FeatureToggleMiddleware"]
