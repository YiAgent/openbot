# openbot/infrastructure/agents/_budget_middleware.py
"""Per-task budget guard for the DeepAgents loop — closure-followup workstream 4.

Mirrors the shape of ``ToolCallRepetitionGuard`` in ``_middleware.py``:
one ``AgentMiddleware`` registered at runtime build time, intercepting both
LLM calls (``awrap_model_call``) and tool calls (``awrap_tool_call``).

Pre-check semantics:
  * Before each LLM/tool dispatch, re-read ``cost_meter.sum_recorded_for_task``.
  * If the running total ≥ ``per_task_cap_usd``, terminate the loop with a
    bounded synthetic message — the model never gets to call again.
  * On any exception from the cost-meter read, log + continue. A flaky cost
    store must not stop legitimate work.

State sharing:
  ``BudgetGuardState`` is constructed once per ``run`` invocation in
  ``runtime.py`` and passed into the guard. The guard mutates ``partial`` and
  ``exceeded_reason`` so the caller can flip ``partial=True`` on the domain
  result and write a single audit row.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage

logger = logging.getLogger(__name__)

BUDGET_EXCEEDED_REASON = "budget_exceeded_in_loop"

_USER_MESSAGE_TEMPLATE = (
    "Per-task budget exceeded (${spent} / ${cap}); stopping. "
    "Returning partial result. (audit_reason={reason})"
)


@dataclass
class BudgetGuardState:
    """Mutable companion to BudgetGuard, owned by the caller (runtime.run).

    Constructed once per agent run; the guard reads ``cap_usd`` + ``task_id``
    + ``lookup`` and writes ``partial`` + ``exceeded_reason``.
    """

    task_id: str
    cap_usd: Decimal
    lookup: Callable[[], Awaitable[Decimal]]
    partial: bool = False
    exceeded_reason: str | None = None
    _trip_emitted: bool = field(default=False, repr=False)


class BudgetGuard(AgentMiddleware):
    """LangChain agent middleware: terminate the loop when over the per-task cap."""

    def __init__(self, *, state: BudgetGuardState) -> None:
        super().__init__()
        self._state = state

    async def _is_over_cap(self) -> tuple[bool, Decimal]:
        try:
            spent = await self._state.lookup()
        except Exception:
            logger.warning(
                "budget_guard_lookup_failed_failopen",
                exc_info=True,
                extra={"task_id": self._state.task_id},
            )
            return False, Decimal("0")
        return (spent >= self._state.cap_usd, spent)

    def _trip(self, spent: Decimal) -> str:
        """Mutate state and return the user-visible message."""
        self._state.partial = True
        self._state.exceeded_reason = BUDGET_EXCEEDED_REASON
        message = _USER_MESSAGE_TEMPLATE.format(
            spent=spent,
            cap=self._state.cap_usd,
            reason=BUDGET_EXCEEDED_REASON,
        )
        if not self._state._trip_emitted:
            logger.warning(
                "budget_guard_tripped",
                extra={
                    "task_id": self._state.task_id,
                    "spent_usd": str(spent),
                    "cap_usd": str(self._state.cap_usd),
                },
            )
            self._state._trip_emitted = True
        return message

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        over, spent = await self._is_over_cap()
        if not over:
            return await handler(request)
        # Inject a terminating AIMessage into the graph state. Returning a
        # dict with messages tells deepagents/LangGraph that the model
        # produced a final answer, which is the cheapest way to end the loop.
        return {"messages": [AIMessage(content=self._trip(spent))]}

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        over, spent = await self._is_over_cap()
        if not over:
            return await handler(request)
        # For tool calls, return a ToolMessage with status=error — the
        # graph treats this as a terminated tool branch and the model gets
        # a clear "stop" signal on the next step (which we'll also block).
        return ToolMessage(
            content=self._trip(spent),
            tool_call_id=request.tool_call.get("id", ""),
            name=request.tool_call.get("name", ""),
            status="error",
        )


class _DeferredBudgetGuard(BudgetGuard):
    """Pre-stack-time placeholder; ``runtime.run`` rebinds ``self._state`` per run.

    The stack is built once per profile; each ``run`` call mutates the state
    via ``bind_state`` so concurrent runs don't share counters.
    """

    def __init__(self) -> None:
        # Construct with a no-op state — the cap is unreachable until rebound.
        super().__init__(
            state=BudgetGuardState(
                task_id="<unbound>",
                cap_usd=Decimal("999999"),
                lookup=_zero_lookup,
            )
        )

    def bind_state(self, state: BudgetGuardState) -> None:
        self._state = state


async def _zero_lookup() -> Decimal:
    return Decimal("0")


def make_budget_guard() -> _DeferredBudgetGuard:
    return _DeferredBudgetGuard()


__all__ = [
    "BUDGET_EXCEEDED_REASON",
    "BudgetGuard",
    "BudgetGuardState",
    "_DeferredBudgetGuard",
    "make_budget_guard",
]
