"""BudgetGuard — pre-LLM and pre-tool spend check."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage

from openbot.infrastructure.agents._budget_middleware import (
    BUDGET_EXCEEDED_REASON,
    BudgetGuard,
    BudgetGuardState,
)


def _state(spent: Decimal, cap: Decimal = Decimal("0.10")) -> BudgetGuardState:
    return BudgetGuardState(task_id="t-1", cap_usd=cap, lookup=AsyncMock(return_value=spent))


async def test_pre_llm_blocks_when_over_cap() -> None:
    state = _state(Decimal("0.11"))
    guard = BudgetGuard(state=state)

    async def handler(_request: Any) -> Any:
        raise AssertionError("LLM call must not run when budget exceeded")

    request = {"messages": []}
    result = await guard.awrap_model_call(request, handler)
    assert isinstance(result, dict)
    msgs = result["messages"]
    assert isinstance(msgs[-1], AIMessage)
    assert "Per-task budget exceeded" in msgs[-1].content
    assert state.partial is True
    assert state.exceeded_reason == BUDGET_EXCEEDED_REASON


async def test_pre_llm_passes_when_under_cap() -> None:
    state = _state(Decimal("0.05"))
    guard = BudgetGuard(state=state)
    sentinel = {"messages": [AIMessage(content="ok")]}

    async def handler(_request: Any) -> Any:
        return sentinel

    out = await guard.awrap_model_call({"messages": []}, handler)
    assert out is sentinel
    assert state.partial is False


async def test_pre_tool_blocks_when_over_cap() -> None:
    state = _state(Decimal("0.20"))
    guard = BudgetGuard(state=state)

    request = ToolCallRequest(
        tool_call={"name": "read_file", "args": {"path": "x"}, "id": "t1"},
        tool=None,  # type: ignore[arg-type]
        state={},
        runtime=None,  # type: ignore[arg-type]
    )

    async def handler(_r: ToolCallRequest) -> Any:
        raise AssertionError("tool must not run")

    out = await guard.awrap_tool_call(request, handler)
    # Returns a ToolMessage error so the loop terminates cleanly.
    assert getattr(out, "status", None) == "error"
    assert "Per-task budget exceeded" in out.content
    assert state.partial is True


async def test_failsafe_on_meter_error() -> None:
    state = BudgetGuardState(
        task_id="t-1",
        cap_usd=Decimal("0.10"),
        lookup=AsyncMock(side_effect=RuntimeError("DB down")),
    )
    guard = BudgetGuard(state=state)
    sentinel = object()

    async def handler(_r: Any) -> Any:
        return sentinel

    out = await guard.awrap_model_call({"messages": []}, handler)
    assert out is sentinel
    assert state.partial is False
