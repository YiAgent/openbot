"""Integration tests for the run_preflight chain.

These tests wire real middleware implementations (KillSwitchMiddleware,
FeatureToggleMiddleware, RateLimitMiddleware, AuditStartMiddleware) together
through the real run_preflight runner.  No network I/O; FakeChannelAdapter
and FakeAuditLog are used for side-effects.

Design rule: each test exercises at least one real Middleware + the real
run_preflight runner so the short-circuit behaviour is verified end-to-end.
"""

from __future__ import annotations

import pytest

from openbot.application.middleware import (
    AuditStartMiddleware,
    FeatureToggleMiddleware,
    KillSwitchMiddleware,
    MiddlewareResult,
    PreflightContext,
    RateLimitMiddleware,
    run_preflight,
)
from openbot.application.router import Dispatch, SandboxPolicy
from openbot.domain.workflows import Feature
from openbot.infrastructure.config_loader import baked_in_defaults
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes import FakeAuditLog, FakeChannelAdapter, FakeRateLimiter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    *,
    feature: Feature = Feature.TRIAGE,
    audit: FakeAuditLog | None = None,
    rate_limiter: FakeRateLimiter | None = None,
    env: dict[str, str] | None = None,  # unused; handled via monkeypatch in each test
) -> PreflightContext:
    """Build a minimal PreflightContext backed by fakes."""
    event = build_issue_opened_event(issue_number=1)
    adapter = FakeChannelAdapter()
    config = baked_in_defaults()

    async def _stub_handler(ctx: PreflightContext) -> None:
        pass

    dispatch = Dispatch(
        feature=feature,
        handler=_stub_handler,
        task_id="a" * 32,
        sandbox_policy=SandboxPolicy.NO_SANDBOX,
    )
    return PreflightContext(
        event=event,
        dispatch=dispatch,
        config=config,
        adapter=adapter,
        session_factory=None,
        redis=None,
        audit=audit,
        rate_limiter=rate_limiter,
    )


# ---------------------------------------------------------------------------
# KillSwitch tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestKillSwitchMiddleware:
    async def test_kill_switch_off_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When env var is absent, kill switch PROCEEDs."""
        monkeypatch.delenv("OPENBOT_KILL_SWITCH", raising=False)
        ctx = _make_ctx()
        decision = await run_preflight(ctx, [KillSwitchMiddleware()])
        assert decision.result is MiddlewareResult.PROCEED

    async def test_kill_switch_on_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OPENBOT_KILL_SWITCH=true → BLOCKED with reason='kill_switch'."""
        monkeypatch.setenv("OPENBOT_KILL_SWITCH", "true")
        ctx = _make_ctx()
        decision = await run_preflight(ctx, [KillSwitchMiddleware()])
        assert decision.result is MiddlewareResult.BLOCKED
        assert decision.reason == "kill_switch"

    async def test_kill_switch_block_writes_audit_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kill switch BLOCKED → run_preflight writes exactly one block audit row."""
        monkeypatch.setenv("OPENBOT_KILL_SWITCH", "true")
        audit = FakeAuditLog()
        ctx = _make_ctx(audit=audit)
        await run_preflight(ctx, [KillSwitchMiddleware(), AuditStartMiddleware()])
        # run_preflight itself writes the block row; AuditStartMiddleware never ran
        assert len(audit.entries) == 1
        assert audit.entries[0].outcome == "kill_switch"


# ---------------------------------------------------------------------------
# FeatureToggle tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFeatureToggleMiddleware:
    async def test_feature_enabled_proceeds(self) -> None:
        """All features enabled in default config → PROCEED."""
        ctx = _make_ctx(feature=Feature.TRIAGE)
        decision = await run_preflight(ctx, [FeatureToggleMiddleware()])
        assert decision.result is MiddlewareResult.PROCEED

    async def test_run_preflight_short_circuits_on_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First BLOCKED middleware stops the chain; runner writes one block row."""
        monkeypatch.setenv("OPENBOT_KILL_SWITCH", "true")
        audit = FakeAuditLog()
        ctx = _make_ctx(audit=audit)
        # kill_switch BLOCKS → feature_toggle + audit_start never run
        chain = [KillSwitchMiddleware(), FeatureToggleMiddleware(), AuditStartMiddleware()]
        decision = await run_preflight(ctx, chain)
        assert decision.result is MiddlewareResult.BLOCKED
        assert decision.reason == "kill_switch"
        # run_preflight writes exactly the block audit row; AuditStart never ran
        assert len(audit.entries) == 1
        assert audit.entries[0].outcome == "kill_switch"


# ---------------------------------------------------------------------------
# RateLimit tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRateLimitMiddleware:
    async def test_non_chat_feature_always_proceeds(self) -> None:
        """Rate limiter only applies to CHAT; TRIAGE proceeds unconditionally."""
        rate_limiter = FakeRateLimiter(responses=[False])  # would block if checked
        ctx = _make_ctx(feature=Feature.TRIAGE, rate_limiter=rate_limiter)
        decision = await run_preflight(ctx, [RateLimitMiddleware()])
        assert decision.result is MiddlewareResult.PROCEED

    async def test_chat_no_rate_limiter_proceeds(self) -> None:
        """Chat with no rate_limiter falls open → PROCEED."""
        ctx = _make_ctx(feature=Feature.CHAT, rate_limiter=None)
        decision = await run_preflight(ctx, [RateLimitMiddleware()])
        assert decision.result is MiddlewareResult.PROCEED


# ---------------------------------------------------------------------------
# AuditStart tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAuditStartMiddleware:
    async def test_audit_start_records_entry_when_audit_set(self) -> None:
        """AuditStartMiddleware writes a log entry if ctx.audit is set."""
        audit = FakeAuditLog()
        ctx = _make_ctx(audit=audit)
        decision = await run_preflight(ctx, [AuditStartMiddleware()])
        assert decision.result is MiddlewareResult.PROCEED
        assert len(audit.entries) == 1

    async def test_audit_start_without_audit_proceeds(self) -> None:
        """AuditStartMiddleware falls open gracefully when ctx.audit is None."""
        ctx = _make_ctx(audit=None)
        decision = await run_preflight(ctx, [AuditStartMiddleware()])
        assert decision.result is MiddlewareResult.PROCEED


# ---------------------------------------------------------------------------
# Chain composition tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPreflightChainComposition:
    async def test_all_proceed_returns_proceed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Full chain (no blocks) → PROCEED, audit entry written."""
        monkeypatch.delenv("OPENBOT_KILL_SWITCH", raising=False)
        audit = FakeAuditLog()
        ctx = _make_ctx(audit=audit)
        chain = [KillSwitchMiddleware(), FeatureToggleMiddleware(), AuditStartMiddleware()]
        decision = await run_preflight(ctx, chain)
        assert decision.result is MiddlewareResult.PROCEED
        assert len(audit.entries) == 1

    async def test_middleware_exception_does_not_propagate(self) -> None:
        """A buggy middleware must not raise; run_preflight continues."""

        class BuggyMiddleware:
            name = "buggy"

            async def __call__(self, ctx: PreflightContext) -> None:  # type: ignore[return]
                raise RuntimeError("boom")

        ctx = _make_ctx()
        # Should not raise; should fall through to PROCEED
        decision = await run_preflight(ctx, [BuggyMiddleware()])  # type: ignore[list-item]
        assert decision.result is MiddlewareResult.PROCEED
