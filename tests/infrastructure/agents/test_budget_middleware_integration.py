"""Integration: BudgetGuard wired into BaseDeepAgentRuntime stops a runaway loop."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openbot.infrastructure.agents._budget_middleware import (
    BUDGET_EXCEEDED_REASON,
    BudgetGuard,
    BudgetGuardState,
)


async def test_state_trips_after_first_overcap_check() -> None:
    """Sanity: a sequence of 100 lookups that all return over-cap stops at #1
    and never reads further once the partial flag is set."""
    state = BudgetGuardState(
        task_id="t-1",
        cap_usd=Decimal("0.10"),
        lookup=AsyncMock(return_value=Decimal("0.11")),
    )
    guard = BudgetGuard(state=state)
    handler = AsyncMock()
    for _ in range(100):
        await guard.awrap_model_call({"messages": []}, handler)
    # Handler must never have been called.
    assert handler.call_count == 0
    assert state.partial is True
    assert state.exceeded_reason == BUDGET_EXCEEDED_REASON


async def test_runtime_prepends_budget_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime stack must contain BudgetGuard *before* ToolCallRepetitionGuard."""
    from openbot.infrastructure.agents import runtime as rt

    captured: dict[str, Any] = {}

    def fake_build(limits: Any) -> list[Any]:
        # Re-use the real function but capture the resulting list.
        out = rt._build_standard_middleware(limits)
        captured["middleware"] = out
        return out

    # Build once with a representative limits object.
    from openbot.infrastructure.agents.profiles import AgentRunLimits

    stack = fake_build(AgentRunLimits(recursion_limit=30, model_call_limit=5, tool_call_limit=20))
    names = [type(m).__name__ for m in stack]
    # Guard ordering: BudgetGuard (or _DeferredBudgetGuard) first, then ToolCallRepetitionGuard,
    # then the limit middlewares.
    assert names[0] in ("BudgetGuard", "_DeferredBudgetGuard"), names
