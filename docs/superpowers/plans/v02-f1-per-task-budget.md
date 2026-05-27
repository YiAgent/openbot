# F1: Per-Task Budget Fix

> Sprint 1, Task T1. Fix: per-task budget only checked at preflight (input-side), not during agent loop execution.

## Problem

`BudgetEnforcement` middleware checks monthly/global caps before the task starts, but the per-task cap ($3.00 for fix) is never enforced during the agent loop. A runaway agent can exceed its budget.

## Solution

Add `BudgetStepGuard` — a middleware that checks cumulative cost before each agent step.

### File: `openbot/infrastructure/agents/_budget_middleware.py`

```python
class BudgetStepGuard:
    """Check per-task cost before each agent step.

    Queries cost_meter for cumulative spend on this task_id.
    If exceeds per_task_cap_usd → raise BudgetExceeded.
    """

    def __init__(self, task_id: str, cap_usd: float, session_factory):
        ...

    async def before_step(self, state) -> None:
        total = await self._query_cost()
        if total >= self.cap_usd:
            raise BudgetExceeded(f"Hit per-task budget (${self.cap_usd:.2f})")
```

### Integration Point

Wire into DeepAgents runtime middleware stack in `openbot/infrastructure/agents/runtime.py`.

### Tests

- `tests/unit/infrastructure/agents/test_budget_step_guard.py`
  - Under budget → continues
  - At/over budget → raises BudgetExceeded
  - cost_meter query failure → degrades gracefully (log warning, continue)
