"""FeatureToggleMiddleware — `.openbot/config.yaml` features.* gate."""

from __future__ import annotations

from dataclasses import replace

import pytest

from openbot.application.middleware import MiddlewareResult
from openbot.application.middleware.feature_toggle import FeatureToggleMiddleware
from openbot.domain.workflows import Feature
from openbot.infrastructure.config_loader import baked_in_defaults
from openbot.infrastructure.persistence.models import WorkflowPhase
from tests.application.middleware.conftest import make_ctx


def _config_with(*, triage=True, review=True, fix=True, chat=True):
    """``EffectiveConfig`` clone with overridden feature flags."""
    base = baked_in_defaults()
    features = replace(
        base.features,
        triage=triage,
        review=review,
        fix=fix,
        chat=chat,
    )
    return replace(base, features=features)


# ───── default (all-true) PROCEEDs ─────


@pytest.mark.parametrize("feature", list(Feature))
async def test_default_features_all_proceed(feature: Feature) -> None:
    """Baked-in defaults enable every feature."""
    ctx = make_ctx(feature=feature)
    decision = await FeatureToggleMiddleware()(ctx)
    assert decision.result is MiddlewareResult.PROCEED


# ───── disabled feature BLOCKED ─────


@pytest.mark.parametrize(
    ("feature", "flag_kwarg"),
    [
        (Feature.TRIAGE, "triage"),
        (Feature.REVIEW, "review"),
        (Feature.FIX, "fix"),
        (Feature.CHAT, "chat"),
    ],
)
async def test_disabled_feature_blocks_with_namespaced_reason(
    feature: Feature, flag_kwarg: str
) -> None:
    """``features.{feature}=false`` → BLOCKED, audit SKIPPED, no comment."""
    config = _config_with(**{flag_kwarg: False})
    ctx = make_ctx(feature=feature, config=config)
    decision = await FeatureToggleMiddleware()(ctx)
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == f"feature_disabled:{feature.value}"
    assert decision.audit_phase is WorkflowPhase.SKIPPED
    # User-disabled — no notification reply.
    assert decision.comment is None


async def test_only_target_feature_blocked_when_one_disabled() -> None:
    """Disabling chat does not affect triage."""
    config = _config_with(chat=False)
    triage_decision = await FeatureToggleMiddleware()(
        make_ctx(feature=Feature.TRIAGE, config=config)
    )
    chat_decision = await FeatureToggleMiddleware()(make_ctx(feature=Feature.CHAT, config=config))
    assert triage_decision.result is MiddlewareResult.PROCEED
    assert chat_decision.result is MiddlewareResult.BLOCKED
