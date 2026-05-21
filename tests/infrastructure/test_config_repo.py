"""`.openbot/config.yaml` loader — harness spec §3 M1."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.domain.workflows import Feature
from openbot.infrastructure.config_loader import (
    EffectiveConfig,
    baked_in_defaults,
    clear_cache,
    load_for_repo,
)

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


def _make_adapter(
    file_bytes: bytes | None,
    *,
    raises: Exception | None = None,
) -> AsyncMock:
    """Build an adapter mock exposing only `fetch_repo_file` (the ChannelAdapterPort method).

    Args:
        file_bytes: The raw bytes that `fetch_repo_file` returns. Pass None to simulate 404.
        raises: If set, `fetch_repo_file` raises this exception instead.
    """
    adapter = AsyncMock()
    if raises is not None:
        adapter.fetch_repo_file = AsyncMock(side_effect=raises)
    else:
        adapter.fetch_repo_file = AsyncMock(return_value=file_bytes)
    return adapter


def _yaml_bytes(yaml_text: str) -> bytes:
    return yaml_text.encode("utf-8")


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
    adapter = _make_adapter(None)  # fetch_repo_file returns None → 404
    cfg = await load_for_repo(adapter, _event())
    assert cfg == baked_in_defaults()


async def test_returns_defaults_on_5xx() -> None:
    import httpx

    adapter = _make_adapter(
        None,
        raises=httpx.HTTPStatusError(
            "502", request=httpx.Request("GET", "https://x"), response=httpx.Response(502)
        ),
    )
    cfg = await load_for_repo(adapter, _event())
    assert cfg == baked_in_defaults()


async def test_returns_defaults_when_installation_id_missing() -> None:
    adapter = _make_adapter(_yaml_bytes("cancel:\n  label: x\n"))
    cfg = await load_for_repo(adapter, _event(installation_id=None))
    assert cfg == baked_in_defaults()
    adapter.fetch_repo_file.assert_not_called()


async def test_returns_defaults_on_token_error() -> None:
    adapter = _make_adapter(
        None,
        raises=RuntimeError("App auth not configured"),
    )
    cfg = await load_for_repo(adapter, _event())
    assert cfg == baked_in_defaults()


async def test_returns_defaults_on_http_error() -> None:
    import httpx

    adapter = _make_adapter(None, raises=httpx.ConnectError("no route"))
    cfg = await load_for_repo(adapter, _event())
    assert cfg == baked_in_defaults()


async def test_returns_defaults_on_invalid_yaml() -> None:
    # Tabs at column 0 + unmatched bracket — guaranteed YAMLError.
    adapter = _make_adapter(_yaml_bytes("budget: [unclosed"))
    cfg = await load_for_repo(adapter, _event())
    assert cfg == baked_in_defaults()


async def test_returns_defaults_when_yaml_root_is_list() -> None:
    adapter = _make_adapter(_yaml_bytes("- a\n- b\n"))
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
    adapter = _make_adapter(_yaml_bytes(yaml_text))
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
    adapter = _make_adapter(_yaml_bytes(yaml_text))
    cfg = await load_for_repo(adapter, _event())
    assert cfg.cancel.label == "halt-bot"
    assert cfg.cancel.comment_keywords == ("halt", "abort")


async def test_severity_threshold_validated_against_enum() -> None:
    yaml_text = "review:\n  severity_threshold: bogus\n"
    adapter = _make_adapter(_yaml_bytes(yaml_text))
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
    adapter = _make_adapter(_yaml_bytes(yaml_text))
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
    adapter = _make_adapter(_yaml_bytes(yaml_text))
    cfg = await load_for_repo(adapter, _event())
    # Bad fix value → default; rest of budget untouched.
    assert cfg.budget.per_task[Feature.FIX] == Decimal("3.00")


# ───── cache (§3 M1 acceptance: "10 calls → 1 API hit") ─────


async def test_cache_coalesces_repeated_calls() -> None:
    adapter = _make_adapter(_yaml_bytes("cancel:\n  label: x\n"))
    for _ in range(10):
        await load_for_repo(adapter, _event())
    assert adapter.fetch_repo_file.await_count == 1


async def test_cache_is_keyed_per_repo() -> None:
    adapter = _make_adapter(_yaml_bytes("cancel:\n  label: y\n"))
    await load_for_repo(adapter, _event(repo="org/a"))
    await load_for_repo(adapter, _event(repo="org/b"))
    assert adapter.fetch_repo_file.await_count == 2


# ───── safety guarantees ─────


def test_module_uses_safe_load_not_load() -> None:
    """Static guard: `yaml.load` (CWE-502 unsafe) must never appear in this module."""
    import pathlib

    from openbot.infrastructure import config_loader as config_repo

    src = pathlib.Path(config_repo.__file__ or "")
    text = src.read_text(encoding="utf-8")
    # `yaml.safe_load` is fine; bare `yaml.load(` is what we must reject.
    # Strip out `safe_load` mentions then look for any remaining `load(`.
    assert "yaml.load(" not in text.replace("yaml.safe_load", "")


def test_effective_config_type_safety() -> None:
    """EffectiveConfig is the only return shape — no leaking dict / None."""
    c = baked_in_defaults()
    assert isinstance(c, EffectiveConfig)
