"""Tests for ConfigApprovalGate middleware."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openbot.application.middleware.config_approval import ConfigApprovalGate
from openbot.application.middleware.preflight import MiddlewareResult
from openbot.domain.events import EventKind, UnifiedEvent


def _make_event(
    *,
    pr_number: int | None = None,
    pr_body: str = "",
    pr_title: str = "",
    labels: list[str] | None = None,
) -> UnifiedEvent:
    raw = {}
    if pr_number is not None:
        raw["pull_request"] = {
            "body": pr_body,
            "title": pr_title,
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


def _make_ctx(event: UnifiedEvent) -> MagicMock:
    ctx = MagicMock()
    ctx.event = event
    return ctx


@pytest.mark.asyncio
async def test_non_pr_returns_proceed():
    gate = ConfigApprovalGate()
    event = _make_event()  # no pr_number
    ctx = _make_ctx(event)
    decision = await gate(ctx)
    assert decision.result == MiddlewareResult.PROCEED


@pytest.mark.asyncio
async def test_pr_without_config_mention_returns_proceed():
    gate = ConfigApprovalGate()
    event = _make_event(pr_number=42, pr_body="Fix a bug", pr_title="Fix bug")
    ctx = _make_ctx(event)
    decision = await gate(ctx)
    assert decision.result == MiddlewareResult.PROCEED


@pytest.mark.asyncio
async def test_config_pr_without_approval_label_blocked():
    gate = ConfigApprovalGate()
    event = _make_event(
        pr_number=42,
        pr_body="Update .openbot/config.yaml budget settings",
        pr_title="Config update",
    )
    ctx = _make_ctx(event)
    decision = await gate(ctx)
    assert decision.result == MiddlewareResult.BLOCKED
    assert "config-approved" in decision.comment


@pytest.mark.asyncio
async def test_config_pr_with_approval_label_proceeds():
    gate = ConfigApprovalGate()
    event = _make_event(
        pr_number=42,
        pr_body="Update .openbot/config.yaml budget settings",
        pr_title="Config update",
        labels=["config-approved"],
    )
    ctx = _make_ctx(event)
    decision = await gate(ctx)
    assert decision.result == MiddlewareResult.PROCEED


@pytest.mark.asyncio
async def test_config_pr_with_other_labels_blocked():
    gate = ConfigApprovalGate()
    event = _make_event(
        pr_number=42,
        pr_body="Update .openbot/config.yaml safety settings",
        labels=["enhancement", "priority/high"],
    )
    ctx = _make_ctx(event)
    decision = await gate(ctx)
    assert decision.result == MiddlewareResult.BLOCKED
    assert decision.reason == "config_approval_required"
