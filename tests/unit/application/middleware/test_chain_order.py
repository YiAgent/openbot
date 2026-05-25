"""Unit test for openbot.application.dispatcher middleware chain order.

The chain order is a spec §3 M3 contract — reordering requires a spec
amendment. This test pins the exact sequence so an accidental reorder
fails CI rather than failing silently in production.
"""

from __future__ import annotations

import pytest

from openbot.application.dispatcher import build_preflight_chain as _build_preflight_chain
from openbot.application.middleware import (
    ActorRoleMiddleware,
    AuditStartMiddleware,
    BudgetMiddleware,
    CancelCommentMiddleware,
    CancelLabelMiddleware,
    FeatureToggleMiddleware,
    ForkPRGateMiddleware,
    KillSwitchMiddleware,
    RateLimitMiddleware,
    SanitizeInputsMiddleware,
)

# Canonical order from spec §3 M3 / dispatcher.py docstring.
_EXPECTED_CHAIN: tuple[type, ...] = (
    SanitizeInputsMiddleware,
    KillSwitchMiddleware,
    FeatureToggleMiddleware,
    CancelLabelMiddleware,
    CancelCommentMiddleware,
    ForkPRGateMiddleware,
    ActorRoleMiddleware,
    RateLimitMiddleware,
    BudgetMiddleware,
    AuditStartMiddleware,
)


@pytest.mark.unit
class TestChainOrder:
    def test_chain_length(self) -> None:
        chain = _build_preflight_chain()
        assert len(chain) == len(_EXPECTED_CHAIN)

    def test_chain_types_in_order(self) -> None:
        chain = _build_preflight_chain()
        for i, (actual, expected_type) in enumerate(zip(chain, _EXPECTED_CHAIN, strict=True)):
            assert isinstance(actual, expected_type), (
                f"Position {i}: expected {expected_type.__name__}, got {type(actual).__name__}"
            )

    def test_fresh_chain_each_call(self) -> None:
        """_build_preflight_chain must return a new list, not a singleton."""
        c1 = _build_preflight_chain()
        c2 = _build_preflight_chain()
        assert c1 is not c2
