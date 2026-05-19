"""LiteLLM `complete()` wrapper — calls LiteLLM, persists cost, returns result.

Stubs `litellm.acompletion` + `litellm.completion_cost` so no real LLM is hit.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from openbot.infrastructure.llm import Feature
from openbot.infrastructure.llm.complete import CompletionResult, complete
from openbot.infrastructure.persistence import (
    CostMeterRepo,
    create_schema,
    make_engine,
    make_session_factory,
    session_scope,
)

# ───── fixtures ─────


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    yield make_session_factory(engine)
    await engine.dispose()


def _fake_response(*, content: str = "labels: bug", prompt: int = 50, completion: int = 8) -> Any:
    """Object shaped like a LiteLLM ModelResponse for what `complete()` reads."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    return SimpleNamespace(choices=[choice], usage=usage)


def _patch_litellm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: Any | None = None,
    cost: float = 0.001,
) -> dict[str, Any]:
    """Stub `litellm.acompletion` + `litellm.completion_cost`.

    Returns a captured-calls dict so tests can assert on what was sent.
    """
    captured: dict[str, Any] = {"calls": []}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured["calls"].append(kwargs)
        return response if response is not None else _fake_response()

    def fake_cost(*, completion_response: Any) -> float:
        return cost

    import openbot.infrastructure.llm.complete as mod

    monkeypatch.setattr(mod.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(mod.litellm, "completion_cost", fake_cost)
    return captured


# ───── happy path ─────


async def test_complete_records_cost_row(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_litellm(monkeypatch, cost=0.012345)

    async with session_scope(factory) as session:
        result = await complete(
            Feature.TRIAGE,
            [{"role": "user", "content": "is this a bug?"}],
            repo="YiAgent/openbot",
            task_id="deliv-1",
            session=session,
        )

    assert isinstance(result, CompletionResult)
    assert result.cost_usd == Decimal("0.012345")
    assert result.model == "anthropic/claude-sonnet-4-6"  # PRD §13 #2 triage route
    assert result.prompt_tokens == 50
    assert result.completion_tokens == 8

    # Verify row persisted.
    async with session_scope(factory) as session:
        total = await CostMeterRepo(session).sum_for_task("deliv-1")
        assert total == Decimal("0.012345")


async def test_complete_routes_review_to_opus(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_litellm(monkeypatch)

    async with session_scope(factory) as session:
        await complete(
            Feature.REVIEW,
            [{"role": "user", "content": "review this diff"}],
            repo="x/y",
            task_id="t-1",
            session=session,
        )

    # PRD §13 #2: review → claude-opus-4-7
    assert captured["calls"][0]["model"] == "anthropic/claude-opus-4-7"


# ───── session is required ─────


async def test_complete_requires_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """`session` is required. Production code paths must not accidentally
    drop cost rows by forgetting a dependency injection.
    """
    _patch_litellm(monkeypatch)

    with pytest.raises(TypeError, match="session"):
        await complete(  # type: ignore[call-arg]
            Feature.CHAT,
            [{"role": "user", "content": "?"}],
            repo="x/y",
            task_id="dry-1",
        )


# ───── response shape resilience ─────


async def test_missing_usage_flags_row_and_warns(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When response.usage is missing, tokens default to 0 BUT the cost row
    is flagged USAGE_MISSING + a WARNING is logged. BudgetEnforcement can
    then distinguish "real zero" from "we don't know"."""
    from openbot.infrastructure.persistence import CostMeter, CostStatus

    response_without_usage = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
    )
    _patch_litellm(monkeypatch, response=response_without_usage, cost=0.0)

    with caplog.at_level(logging.WARNING, logger="openbot.infrastructure.llm.complete"):
        async with session_scope(factory) as session:
            result = await complete(
                Feature.TRIAGE,
                [{"role": "user", "content": "hi"}],
                repo="x/y",
                task_id="no-usage",
                session=session,
            )

    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert result.cost_status is CostStatus.USAGE_MISSING
    assert any(r.message == "llm_usage_missing" for r in caplog.records)

    from sqlalchemy import select

    async with session_scope(factory) as session:
        row = (
            await session.execute(select(CostMeter).where(CostMeter.task_id == "no-usage"))
        ).scalar_one()
        assert row.cost_status is CostStatus.USAGE_MISSING


async def test_none_cost_flags_priced_zero(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LiteLLM returning `cost=None` (unknown model) → Decimal(0) + PRICED_ZERO.

    Previously this would crash with `Decimal(str(None))` → InvalidOperation;
    review caught that real bug.
    """
    from openbot.infrastructure.persistence import CostStatus

    _patch_litellm(monkeypatch, cost=None)

    with caplog.at_level(logging.WARNING, logger="openbot.infrastructure.llm.complete"):
        async with session_scope(factory) as session:
            result = await complete(
                Feature.TRIAGE,
                [{"role": "user", "content": "hi"}],
                repo="x/y",
                task_id="unknown-model",
                session=session,
            )

    assert result.cost_usd == Decimal("0")
    assert result.cost_status is CostStatus.PRICED_ZERO
    assert any(r.message == "litellm_cost_unknown_model" for r in caplog.records)


async def test_unexpected_cost_exception_propagates(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RuntimeError / unknown exceptions from completion_cost MUST propagate.

    A broad `except Exception` here would silently zero out cost forever
    if LiteLLM's API drifts. Defending against that explicitly: only the
    known pricing-error types are caught.
    """
    import openbot.infrastructure.llm.complete as mod

    async def fake_acompletion(**_: Any) -> Any:
        return _fake_response()

    def boom(**_: Any) -> float:
        raise RuntimeError("unexpected internal bug")

    monkeypatch.setattr(mod.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(mod.litellm, "completion_cost", boom)

    with pytest.raises(RuntimeError, match="unexpected"):
        async with session_scope(factory) as session:
            await complete(
                Feature.TRIAGE,
                [{"role": "user", "content": "hi"}],
                repo="x/y",
                task_id="t",
                session=session,
            )


async def test_completion_result_rejects_negative_cost() -> None:
    """CompletionResult guards against negative values via __post_init__."""
    from openbot.infrastructure.persistence import CostStatus

    with pytest.raises(ValueError, match="cost_usd"):
        CompletionResult(
            content="",
            model="anthropic/claude-sonnet-4-6",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=Decimal("-0.01"),
            cost_status=CostStatus.RECORDED,
        )


async def test_completion_result_rejects_negative_tokens() -> None:
    from openbot.infrastructure.persistence import CostStatus

    with pytest.raises(ValueError, match="token"):
        CompletionResult(
            content="",
            model="anthropic/claude-sonnet-4-6",
            prompt_tokens=-1,
            completion_tokens=5,
            cost_usd=Decimal("0.001"),
            cost_status=CostStatus.RECORDED,
        )


async def test_missing_choices_raises_value_error_after_recording_cost(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content extraction failure raises, but cost row is recorded first
    (vendor has billed regardless). Row carries cost_status=EXTRACTION_FAILED.
    """
    from openbot.infrastructure.persistence import CostStatus

    bad = SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=20, completion_tokens=0))
    _patch_litellm(monkeypatch, response=bad, cost=0.001)

    async with session_scope(factory) as session:
        with pytest.raises(ValueError, match="choices"):
            await complete(
                Feature.TRIAGE,
                [{"role": "user", "content": "hi"}],
                repo="x/y",
                task_id="billed-but-broken",
                session=session,
            )

    # Cost row WAS persisted — extraction failed AFTER vendor billed.
    from sqlalchemy import select

    from openbot.infrastructure.persistence import CostMeter

    async with session_scope(factory) as session:
        result = await session.execute(
            select(CostMeter).where(CostMeter.task_id == "billed-but-broken")
        )
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].cost_status is CostStatus.EXTRACTION_FAILED
        assert rows[0].prompt_tokens == 20


async def test_expected_pricing_error_persists_zero_with_status(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When LiteLLM raises an EXPECTED pricing error (ValueError/KeyError/etc.),
    record `cost_usd=0` with `cost_status=PRICING_FAILED` + log.

    BudgetEnforcement sees this row, filters it out of strict accounting
    (because cost is unknown not zero), but the row's existence is preserved
    so the call is auditable.
    """
    import openbot.infrastructure.llm.complete as mod
    from openbot.infrastructure.persistence import CostStatus

    async def fake_acompletion(**_: Any) -> Any:
        return _fake_response()

    def boom(**_: Any) -> float:
        # ValueError is in _EXPECTED_PRICING_ERRORS → degrade, don't crash.
        raise ValueError("unknown model pricing")

    monkeypatch.setattr(mod.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(mod.litellm, "completion_cost", boom)

    with caplog.at_level(logging.ERROR, logger="openbot.infrastructure.llm.complete"):
        async with session_scope(factory) as session:
            result = await complete(
                Feature.TRIAGE,
                [{"role": "user", "content": "hi"}],
                repo="x/y",
                task_id="t",
                session=session,
            )

    assert result.cost_usd == Decimal("0")
    assert result.cost_status is CostStatus.PRICING_FAILED
    assert any(r.message == "litellm_cost_calc_failed" for r in caplog.records)

    async with session_scope(factory) as session:
        total = await CostMeterRepo(session).sum_for_task("t")
        assert total == Decimal("0")
