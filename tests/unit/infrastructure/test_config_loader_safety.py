"""Loader coverage for the new ``safety`` block + ``budget.per_task_cap_usd``."""

from __future__ import annotations

from decimal import Decimal

import pytest

from openbot.infrastructure.config_loader import _coerce, baked_in_defaults


@pytest.mark.unit
def test_default_safety_redact_action() -> None:
    cfg = baked_in_defaults()
    assert cfg.safety.egress_action == "redact"
    assert cfg.budget.per_task_cap_usd == Decimal("1.50")


def test_yaml_block_action() -> None:
    parsed = {"safety": {"egress_action": "block"}}
    cfg = _coerce(parsed, repo="acme/repo")
    assert cfg.safety.egress_action == "block"


def test_yaml_invalid_action_falls_back() -> None:
    parsed = {"safety": {"egress_action": "shout"}}
    cfg = _coerce(parsed, repo="acme/repo")
    assert cfg.safety.egress_action == "redact"


def test_yaml_per_task_cap_override() -> None:
    parsed = {"budget": {"per_task_cap_usd": "0.75"}}
    cfg = _coerce(parsed, repo="acme/repo")
    assert cfg.budget.per_task_cap_usd == Decimal("0.75")
