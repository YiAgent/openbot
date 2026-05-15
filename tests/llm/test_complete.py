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

from openbot.llm import Feature
from openbot.llm.complete import CompletionResult, complete
from openbot.persistence import (
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

    import openbot.llm.complete as mod

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


# ───── no session — usable from scripts ─────


async def test_complete_runs_without_session_but_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_litellm(monkeypatch, cost=0.003)

    with caplog.at_level(logging.WARNING, logger="openbot.llm.complete"):
        result = await complete(
            Feature.CHAT,
            [{"role": "user", "content": "?"}],
            repo="x/y",
            task_id="dry-1",
            session=None,
        )

    assert result.cost_usd == Decimal("0.003")
    assert any(r.message == "llm_complete_no_session" for r in caplog.records)


# ───── response shape resilience ─────


async def test_missing_usage_treats_as_zero_tokens(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_without_usage = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
    )
    _patch_litellm(monkeypatch, response=response_without_usage, cost=0.0)

    async with session_scope(factory) as session:
        result = await complete(
            Feature.TRIAGE,
            [{"role": "user", "content": "hi"}],
            repo="x/y",
            task_id="t",
            session=session,
        )

    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0


async def test_missing_choices_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = SimpleNamespace(choices=[])  # empty
    _patch_litellm(monkeypatch, response=bad)

    with pytest.raises(ValueError, match="choices"):
        await complete(
            Feature.TRIAGE,
            [{"role": "user", "content": "hi"}],
            repo="x/y",
            task_id="t",
            session=None,
        )


async def test_cost_calc_failure_persists_zero(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When LiteLLM can't price an unknown model, record cost=0 + log.

    Rationale: don't abort the workflow because pricing metadata is missing;
    the safe direction is "no spend recorded" — BudgetEnforcement will then
    see no charge, which lets the next call through but doesn't under-charge
    a real budget.
    """
    import openbot.llm.complete as mod

    async def fake_acompletion(**_: Any) -> Any:
        return _fake_response()

    def boom(**_: Any) -> float:
        raise RuntimeError("unknown model pricing")

    monkeypatch.setattr(mod.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(mod.litellm, "completion_cost", boom)

    with caplog.at_level(logging.ERROR, logger="openbot.llm.complete"):
        async with session_scope(factory) as session:
            result = await complete(
                Feature.TRIAGE,
                [{"role": "user", "content": "hi"}],
                repo="x/y",
                task_id="t",
                session=session,
            )

    assert result.cost_usd == Decimal("0")
    assert any(r.message == "litellm_cost_calc_failed" for r in caplog.records)

    async with session_scope(factory) as session:
        total = await CostMeterRepo(session).sum_for_task("t")
        assert total == Decimal("0")
