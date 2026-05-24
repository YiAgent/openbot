"""LLM router — locks the PRD §13 #2 routing table.

These tests are intentionally rigid: if someone "improves" the routing without
updating PRD §13, the suite tells them. The decision is locked, not advisory.
"""

from __future__ import annotations

import pytest

from openbot.infrastructure.llm import Feature, fallback_model, primary_model_for


@pytest.mark.parametrize(
    ("feature", "expected"),
    [
        (Feature.TRIAGE, "anthropic/glm-5.1"),
        (Feature.CHAT, "anthropic/glm-5.1"),
        (Feature.REVIEW, "anthropic/glm-5.1"),
        (Feature.FIX, "anthropic/glm-5.1"),
    ],
)
def test_primary_routes_lock_prd_13_2(feature: Feature, expected: str) -> None:
    assert primary_model_for(feature) == expected


def test_all_features_have_a_primary() -> None:
    # Adding a feature to the enum without updating the routing table is a
    # locking bug. Caught here.
    for feature in Feature:
        primary_model_for(feature)


def test_fallback_is_gpt5_mini() -> None:
    assert fallback_model() == "openai/gpt-5-mini"


def test_feature_string_values_are_stable() -> None:
    # Stored in DB (cost_meter, audit_log) — changing string values is breaking.
    assert Feature.TRIAGE.value == "triage"
    assert Feature.REVIEW.value == "review"
    assert Feature.FIX.value == "fix"
    assert Feature.CHAT.value == "chat"
