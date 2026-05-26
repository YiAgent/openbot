"""When BudgetGuard trips, the responder must report partial=True
and the audit row records ``budget_exceeded_in_loop``."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from openbot.infrastructure.agents._budget_middleware import (
    BUDGET_EXCEEDED_REASON,
    BudgetGuard,
    BudgetGuardState,
)


@pytest.mark.unit
async def test_state_records_correct_reason_on_trip() -> None:
    state = BudgetGuardState(
        task_id="t-1",
        cap_usd=Decimal("0.10"),
        lookup=AsyncMock(return_value=Decimal("0.50")),
    )
    guard = BudgetGuard(state=state)
    handler = AsyncMock()
    await guard.awrap_model_call({"messages": []}, handler)
    assert state.partial is True
    assert state.exceeded_reason == BUDGET_EXCEEDED_REASON
    assert handler.call_count == 0
