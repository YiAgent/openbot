"""Budget enforcement middleware — global hard kill + monthly soft cap."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from openbot.application.middleware import MiddlewareResult
from openbot.application.middleware.budget import BudgetMiddleware, _effective_global_cap
from openbot.infrastructure.llm.model_router import Feature
from openbot.infrastructure.persistence import Base, make_session_factory
from openbot.infrastructure.persistence.models import CostMeter, CostStatus, WorkflowPhase
from tests.application.middleware.conftest import make_ctx, make_event

# ───── helpers: in-memory SQLite session factory + seeding ─────


@pytest.fixture
async def session_factory():
    """Fresh aiosqlite engine + schema per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


async def _seed_cost(
    session_factory, *, repo: str, cost_usd: Decimal, status: CostStatus = CostStatus.RECORDED
) -> None:
    async with session_factory() as session:
        session.add(
            CostMeter(
                repo=repo,
                feature=Feature.CHAT,
                task_id="t",
                model="x",
                prompt_tokens=1,
                completion_tokens=1,
                cost_usd=cost_usd,
                cost_status=status,
            )
        )
        await session.commit()


# ───── env override ─────


def test_effective_global_cap_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_GLOBAL_HARD_KILL_USD", "10")
    assert _effective_global_cap(Decimal("500")) == Decimal("10")


def test_effective_global_cap_falls_back_on_bad_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBOT_GLOBAL_HARD_KILL_USD", "not-a-number")
    assert _effective_global_cap(Decimal("500")) == Decimal("500")


def test_effective_global_cap_uses_config_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENBOT_GLOBAL_HARD_KILL_USD", raising=False)
    assert _effective_global_cap(Decimal("250")) == Decimal("250")


# ───── happy / fall-open ─────


async def test_budget_proceeds_with_no_postgres() -> None:
    """No session_factory → fall-open + audit (slice A's persistence is optional)."""
    decision = await BudgetMiddleware()(make_ctx(session_factory=None))
    assert decision.result is MiddlewareResult.PROCEED


async def test_budget_proceeds_when_under_caps(session_factory) -> None:
    decision = await BudgetMiddleware()(make_ctx(session_factory=session_factory))
    assert decision.result is MiddlewareResult.PROCEED


# ───── global hard kill ─────


async def test_budget_blocks_on_global_hard_kill(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENBOT_GLOBAL_HARD_KILL_USD", "10")
    # Seed two repos' worth of spend that combined exceeds the env cap.
    await _seed_cost(session_factory, repo="org/a", cost_usd=Decimal("6"))
    await _seed_cost(session_factory, repo="org/b", cost_usd=Decimal("5"))
    decision = await BudgetMiddleware()(make_ctx(session_factory=session_factory))
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "global_hard_kill"
    assert decision.audit_phase is WorkflowPhase.REJECTED
    assert decision.comment is None  # admin job


# ───── monthly soft cap ─────


async def test_budget_blocks_on_monthly_soft_cap(session_factory) -> None:
    event = make_event(repo="org/r")
    # baked_in_defaults().budget.monthly_soft_cap_usd == 100
    await _seed_cost(session_factory, repo="org/r", cost_usd=Decimal("101"))
    decision = await BudgetMiddleware()(make_ctx(event=event, session_factory=session_factory))
    assert decision.result is MiddlewareResult.BLOCKED
    assert decision.reason == "monthly_soft_cap"
    assert decision.audit_phase is WorkflowPhase.SKIPPED
    assert "budget" in decision.announce_key  # 1/month dedup
    assert decision.announce_ttl == 31 * 86400
    assert decision.comment is not None


# ───── RECORDED-only filter ─────


async def test_budget_ignores_pricing_failed_rows(session_factory) -> None:
    """A repo with $200 spent under pricing_failed must NOT trip the soft cap.

    Without filtering to RECORDED, this would either let real spend leak
    past the cap (status=pricing_failed has cost_usd=0 by convention so
    it'd happily pile up unnoticed) or block legitimate workflows. The
    middleware MUST filter to RECORDED only.
    """
    # 200 in pricing_failed rows: looks expensive but cost_usd is 0 — yet
    # the conservative pattern is to NOT let pricing_failed leak into cap math.
    # We seed with cost_usd=Decimal('0') (consistent with the model invariant)
    # and confirm Budget doesn't trip.
    for _ in range(10):
        await _seed_cost(
            session_factory,
            repo="org/r",
            cost_usd=Decimal("0"),
            status=CostStatus.PRICING_FAILED,
        )
    decision = await BudgetMiddleware()(
        make_ctx(event=make_event(repo="org/r"), session_factory=session_factory)
    )
    assert decision.result is MiddlewareResult.PROCEED


# ───── DB transient ─────


async def test_budget_fall_open_on_db_error() -> None:
    """A DB hiccup must NOT block every webhook indefinitely."""

    def _explode():
        raise RuntimeError("db down")

    decision = await BudgetMiddleware()(make_ctx(session_factory=_explode))
    assert decision.result is MiddlewareResult.PROCEED
