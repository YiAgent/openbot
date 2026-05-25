"""Integration tests for the dispatcher module.

Tests verify that build_preflight_chain() produces the correct chain
and that execute_handler wires the middleware runner correctly when
called with fakes.  Real middleware + real runner + fake adapters.
"""

from __future__ import annotations

import pytest

from openbot.application.dispatcher import build_preflight_chain
from openbot.application.middleware import (
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
from openbot.application.middleware.security import ActorRoleMiddleware

# ---------------------------------------------------------------------------
# Chain structure tests (structure, not unit-of-one)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBuildPreflightChain:
    def test_chain_length_is_ten(self) -> None:
        """Exactly 10 middlewares in the chain (spec §3 M3)."""
        assert len(build_preflight_chain()) == 10

    def test_chain_head_is_sanitize(self) -> None:
        """SanitizeInputsMiddleware must be first (byte-level gate)."""
        chain = build_preflight_chain()
        assert isinstance(chain[0], SanitizeInputsMiddleware)

    def test_chain_tail_is_audit_start(self) -> None:
        """AuditStartMiddleware must be last (STARTED row just before handler)."""
        chain = build_preflight_chain()
        assert isinstance(chain[-1], AuditStartMiddleware)

    def test_kill_switch_before_feature_toggle(self) -> None:
        """KillSwitch (position 1) precedes FeatureToggle (position 2)."""
        chain = build_preflight_chain()
        ks_idx = next(i for i, m in enumerate(chain) if isinstance(m, KillSwitchMiddleware))
        ft_idx = next(i for i, m in enumerate(chain) if isinstance(m, FeatureToggleMiddleware))
        assert ks_idx < ft_idx

    def test_budget_before_audit_start(self) -> None:
        """BudgetMiddleware runs before the final AuditStart row."""
        chain = build_preflight_chain()
        budget_idx = next(i for i, m in enumerate(chain) if isinstance(m, BudgetMiddleware))
        audit_idx = next(i for i, m in enumerate(chain) if isinstance(m, AuditStartMiddleware))
        assert budget_idx < audit_idx

    def test_cancel_label_before_fork_pr_gate(self) -> None:
        """CancelLabel populates the labels cache; ForkPRGate reuses it."""
        chain = build_preflight_chain()
        cl_idx = next(i for i, m in enumerate(chain) if isinstance(m, CancelLabelMiddleware))
        fp_idx = next(i for i, m in enumerate(chain) if isinstance(m, ForkPRGateMiddleware))
        assert cl_idx < fp_idx

    def test_actor_role_before_rate_limit(self) -> None:
        """ActorRole caches the role; RateLimit uses it for exempt check."""
        chain = build_preflight_chain()
        ar_idx = next(i for i, m in enumerate(chain) if isinstance(m, ActorRoleMiddleware))
        rl_idx = next(i for i, m in enumerate(chain) if isinstance(m, RateLimitMiddleware))
        assert ar_idx < rl_idx

    def test_all_middleware_have_name_attribute(self) -> None:
        """Every Middleware must expose a ``name`` str (audit + logs contract)."""
        chain = build_preflight_chain()
        for mw in chain:
            assert isinstance(mw.name, str)
            assert len(mw.name) > 0

    def test_cancel_comment_before_rate_limit(self) -> None:
        """CancelComment gate (cheap regex) sits before the Redis RateLimit."""
        chain = build_preflight_chain()
        cc_idx = next(i for i, m in enumerate(chain) if isinstance(m, CancelCommentMiddleware))
        rl_idx = next(i for i, m in enumerate(chain) if isinstance(m, RateLimitMiddleware))
        assert cc_idx < rl_idx
