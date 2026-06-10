"""Tests for ConfigApprovalGate middleware.

The gate keys off the PR **diff** (the real changed files), so the fixtures
supply a fake adapter whose ``get_pr_diff`` returns a unified diff. A PR that
merely mentions the config path in its title/body but doesn't actually touch
the file must NOT be gated — that was the bypass the diff-based check closes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openbot.application.middleware.config_approval import ConfigApprovalGate
from openbot.application.middleware.preflight import MiddlewareResult
from openbot.domain.events import EventKind, UnifiedEvent

_CONFIG_DIFF = """\
diff --git a/.openbot/config.yaml b/.openbot/config.yaml
index 1234567..89abcde 100644
--- a/.openbot/config.yaml
+++ b/.openbot/config.yaml
@@ -1,3 +1,3 @@
 budget:
-  monthly_soft_cap_usd: 50
+  monthly_soft_cap_usd: 500
"""

_UNRELATED_DIFF = """\
diff --git a/README.md b/README.md
index 1234567..89abcde 100644
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
"""


def _make_event(
    *,
    pr_number: int | None = None,
    labels: list[str] | None = None,
) -> UnifiedEvent:
    raw = {}
    if pr_number is not None:
        raw["pull_request"] = {
            "labels": [{"name": lbl} for lbl in (labels or [])],
        }
    return UnifiedEvent(
        channel="github",
        delivery_id="test-delivery",
        kind=EventKind.PR_OPENED,
        repo="owner/repo",
        actor="test-user",
        pr_number=pr_number,
        raw=raw,
    )


def _make_ctx(event: UnifiedEvent, *, diff: str = "") -> MagicMock:
    ctx = MagicMock()
    ctx.event = event

    async def _get_pr_diff(_event, _pr_number):
        return diff

    ctx.adapter.get_pr_diff = _get_pr_diff
    return ctx


@pytest.mark.asyncio
async def test_non_pr_returns_proceed():
    gate = ConfigApprovalGate()
    ctx = _make_ctx(_make_event())  # no pr_number
    decision = await gate(ctx)
    assert decision.result == MiddlewareResult.PROCEED


@pytest.mark.asyncio
async def test_pr_not_touching_config_returns_proceed():
    gate = ConfigApprovalGate()
    ctx = _make_ctx(_make_event(pr_number=42), diff=_UNRELATED_DIFF)
    decision = await gate(ctx)
    assert decision.result == MiddlewareResult.PROCEED


@pytest.mark.asyncio
async def test_config_pr_without_approval_label_blocked():
    gate = ConfigApprovalGate()
    ctx = _make_ctx(_make_event(pr_number=42), diff=_CONFIG_DIFF)
    decision = await gate(ctx)
    assert decision.result == MiddlewareResult.BLOCKED
    assert "config-approved" in decision.comment
    # High-risk key from the changed lines is surfaced in the comment.
    assert "monthly_soft_cap_usd" in decision.comment


@pytest.mark.asyncio
async def test_config_pr_with_approval_label_proceeds():
    gate = ConfigApprovalGate()
    ctx = _make_ctx(
        _make_event(pr_number=42, labels=["config-approved"]),
        diff=_CONFIG_DIFF,
    )
    decision = await gate(ctx)
    assert decision.result == MiddlewareResult.PROCEED


@pytest.mark.asyncio
async def test_config_pr_with_other_labels_blocked():
    gate = ConfigApprovalGate()
    ctx = _make_ctx(
        _make_event(pr_number=42, labels=["enhancement", "priority/high"]),
        diff=_CONFIG_DIFF,
    )
    decision = await gate(ctx)
    assert decision.result == MiddlewareResult.BLOCKED
    assert decision.reason == "config_approval_required"


@pytest.mark.asyncio
async def test_diff_fetch_failure_fails_open():
    """A transient diff-fetch error must not wedge every PR."""
    gate = ConfigApprovalGate()
    event = _make_event(pr_number=42)
    ctx = MagicMock()
    ctx.event = event

    async def _boom(_event, _pr_number):
        raise RuntimeError("github 502")

    ctx.adapter.get_pr_diff = _boom
    decision = await gate(ctx)
    assert decision.result == MiddlewareResult.PROCEED
