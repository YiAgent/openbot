"""`.openbot/config.yaml` loader — harness spec §3 M1."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from openbot.config_repo import (
    EffectiveConfig,
    baked_in_defaults,
    clear_cache,
    load_for_repo,
)
from openbot.events import EventKind, UnifiedEvent
from openbot.llm.router import Feature

# ───── fixtures ─────


def _event(repo: str = "org/r", installation_id: int | None = 5) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d",
        kind=EventKind.ISSUE_OPENED,
        repo=repo,
        actor="a",
        actor_type="User",
        issue_number=1,
        installation_id=installation_id,
    )


@dataclass
class _Token:
    token: str = "stub-token"


def _fake_response(
    *, status_code: int, json_payload: dict[str, Any] | None = None
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_payload,
        request=httpx.Request("GET", "https://api.github.com/x"),
    )


def _make_adapter(response: httpx.Response, *, token_raises: Exception | None = None) -> Any:
    """Build a duck-typed adapter exposing only what load_for_repo needs."""
    adapter = AsyncMock()
    adapter._api_base = "https://api.github.com"
    adapter._headers = lambda token: {"Authorization": f"token {token}"}
    if token_raises is None:
        adapter._installation_token = AsyncMock(return_value=_Token())
    else:
        adapter._installation_token = AsyncMock(side_effect=token_raises)
    adapter._http = AsyncMock()
    adapter._http.get = AsyncMock(return_value=response)
    # Property mocks are tricky: we need to mock the type, or the instance's __class__.
    # Simplest for this test: make _http a real-ish property or just mock the helper.
    adapter._get_http = lambda: adapter._http
    type(adapter)._http_client = property(lambda self: self._get_http())
    return adapter


def _yaml_response(yaml_text: str) -> httpx.Response:
    return _fake_response(
        status_code=200,
        json_payload={
            "content": base64.b64encode(yaml_text.encode()).decode(),
            "encoding": "base64",
        },
    )


@pytest.fixture(autouse=True)
def _isolate_cache() -> None:
    """Ensure each test starts with an empty config cache."""
    clear_cache()


# ───── baked-in defaults ─────


def test_baked_in_defaults_match_prd_section_4() -> None:
    c = baked_in_defaults()
    assert c.budget.global_hard_kill_usd == Decimal("500")
    assert c.budget.monthly_soft_cap_usd == Decimal("100")
    assert c.budget.per_task[Feature.FIX] == Decimal("3.00")
    assert c.cancel.label == "cancel-openbot"
    assert "owner" in c.rate_limit.exempt_roles
    assert "collaborator" in c.rate_limit.exempt_roles
    assert c.fork_pr.run is False
    assert c.severity_threshold == "medium"


def test_baked_in_defaults_are_frozen() -> None:
    """EffectiveConfig is frozen — accidental mutation must raise."""
    c = baked_in_defaults()
    with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError or AttributeError
        c.severity_threshold = "high"  # type: ignore[misc]


# ───── 404 / errors ─────


async def test_returns_defaults_on_404() -> None:
    adapter = _make_adapter(_fake_response(status_code=404))
    cfg = await load_for_repo(adapter, _event())
    assert cfg == baked_in_defaults()


async def test_returns_defaults_on_5xx() -> None:
    adapter = _make_adapter(_fake_response(status_code=502))
    cfg = await load_for_repo(adapter, _event())
    assert cfg == baked_in_defaults()


async def test_returns_defaults_when_installation_id_missing() -> None:
    adapter = _make_adapter(_fake_response(status_code=200))
    cfg = await load_for_repo(adapter, _event(installation_id=None))
    assert cfg == baked_in_defaults()
    adapter._installation_token.assert_not_called()


async def test_returns_defaults_on_token_error() -> None:
    adapter = _make_adapter(
        _fake_response(status_code=200),
        token_raises=RuntimeError("App auth not configured"),
    )
    cfg = await load_for_repo(adapter, _event())
    assert cfg == baked_in_defaults()


async def test_returns_defaults_on_http_error() -> None:
    adapter = _make_adapter(_fake_response(status_code=200))
    adapter._http.get = AsyncMock(side_effect=httpx.ConnectError("no route"))
    cfg = await load_for_repo(adapter, _event())
    assert cfg == baked_in_defaults()


async def test_returns_defaults_on_invalid_yaml() -> None:
    # Tabs at column 0 + unmatched bracket — guaranteed YAMLError.
    adapter = _make_adapter(_yaml_response("budget: [unclosed"))
    cfg = await load_for_repo(adapter, _event())
    assert cfg == baked_in_defaults()


async def test_returns_defaults_when_yaml_root_is_list() -> None:
    adapter = _make_adapter(_yaml_response("- a\n- b\n"))
    cfg = await load_for_repo(adapter, _event())
    assert cfg == baked_in_defaults()


# ───── coercion ─────


async def test_overrides_budget_per_task() -> None:
    yaml_text = """
budget:
  per_task:
    fix: 5.00
    triage: 0.10
  monthly_soft_cap_usd: 250
  global_hard_kill_usd: 999
"""
    adapter = _make_adapter(_yaml_response(yaml_text))
    cfg = await load_for_repo(adapter, _event())
    assert cfg.budget.per_task[Feature.FIX] == Decimal("5.00")
    assert cfg.budget.per_task[Feature.TRIAGE] == Decimal("0.10")
    # Defaults still applied to fields the YAML omitted.
    assert cfg.budget.per_task[Feature.REVIEW] == Decimal("0.50")
    assert cfg.budget.monthly_soft_cap_usd == Decimal("250")
    assert cfg.budget.global_hard_kill_usd == Decimal("999")


async def test_overrides_cancel_label_and_keywords() -> None:
    yaml_text = """
cancel:
  label: halt-bot
  comment_keywords: [halt, abort]
"""
    adapter = _make_adapter(_yaml_response(yaml_text))
    cfg = await load_for_repo(adapter, _event())
    assert cfg.cancel.label == "halt-bot"
    assert cfg.cancel.comment_keywords == ("halt", "abort")


async def test_severity_threshold_validated_against_enum() -> None:
    yaml_text = "review:\n  severity_threshold: bogus\n"
    adapter = _make_adapter(_yaml_response(yaml_text))
    cfg = await load_for_repo(adapter, _event())
    # Unknown value falls back to default rather than crashing.
    assert cfg.severity_threshold == "medium"


async def test_explicit_zero_cap_is_preserved() -> None:
    """Codex slice-A review finding: `value or default` swallows explicit zeros.

    A maintainer who sets `monthly_soft_cap_usd: 0` is asking to disable
    spend on this repo. The coercion must preserve that zero, not silently
    restore the baked-in default and let spend through.
    """
    yaml_text = """
budget:
  monthly_soft_cap_usd: 0
  global_hard_kill_usd: 0
chat:
  rate_limit:
    per_user_per_day: 0
    per_repo_per_hour: 0
    cost_cap_per_task: 0
"""
    adapter = _make_adapter(_yaml_response(yaml_text))
    cfg = await load_for_repo(adapter, _event())
    assert cfg.budget.monthly_soft_cap_usd == Decimal("0")
    assert cfg.budget.global_hard_kill_usd == Decimal("0")
    assert cfg.rate_limit.per_user_per_day == 0
    assert cfg.rate_limit.per_repo_per_hour == 0
    assert cfg.rate_limit.cost_cap_per_task == Decimal("0")


async def test_bad_decimal_falls_back_silently() -> None:
    """Per-field coercion errors don't poison the whole config."""
    yaml_text = """
budget:
  per_task:
    fix: not-a-number
"""
    adapter = _make_adapter(_yaml_response(yaml_text))
    cfg = await load_for_repo(adapter, _event())
    # Bad fix value → default; rest of budget untouched.
    assert cfg.budget.per_task[Feature.FIX] == Decimal("3.00")


# ───── cache (§3 M1 acceptance: "10 calls → 1 API hit") ─────


async def test_cache_coalesces_repeated_calls() -> None:
    adapter = _make_adapter(_yaml_response("cancel:\n  label: x\n"))
    for _ in range(10):
        await load_for_repo(adapter, _event())
    assert adapter._http.get.await_count == 1


async def test_cache_is_keyed_per_repo() -> None:
    adapter = _make_adapter(_yaml_response("cancel:\n  label: y\n"))
    await load_for_repo(adapter, _event(repo="org/a"))
    await load_for_repo(adapter, _event(repo="org/b"))
    assert adapter._http.get.await_count == 2


# ───── safety guarantees ─────


def test_module_uses_safe_load_not_load() -> None:
    """Static guard: `yaml.load` (CWE-502 unsafe) must never appear in this module."""
    import pathlib

    from openbot import config_repo

    src = pathlib.Path(config_repo.__file__ or "")
    text = src.read_text(encoding="utf-8")
    # `yaml.safe_load` is fine; bare `yaml.load(` is what we must reject.
    # Strip out `safe_load` mentions then look for any remaining `load(`.
    assert "yaml.load(" not in text.replace("yaml.safe_load", "")


def test_effective_config_type_safety() -> None:
    """EffectiveConfig is the only return shape — no leaking dict / None."""
    c = baked_in_defaults()
    assert isinstance(c, EffectiveConfig)
