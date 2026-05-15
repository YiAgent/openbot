"""Repository pattern — thin async wrappers around AsyncSession.

Keeps SQL out of workflows / LLM modules. PRD §8.4 audit_log philosophy:
log every workflow phase; the repo encapsulates that with named helpers
so the call sites stay readable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openbot.persistence.models import AuditLog, CostMeter, CostStatus


class CostMeterRepo:
    """Reads + writes for the cost_meter table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        repo: str,
        feature: str,
        task_id: str,
        model: str,
        cost_usd: Decimal,
        prompt_tokens: int,
        completion_tokens: int,
        cost_status: CostStatus = CostStatus.RECORDED,
    ) -> CostMeter:
        """Persist one LLM call. Caller commits the session.

        `cost_status` defaults to RECORDED (price computed from real usage).
        Pass a non-default value when the cost/tokens are best-effort estimates
        — see `CostStatus` for the four degraded states.
        """
        entry = CostMeter(
            repo=repo,
            feature=feature,
            task_id=task_id,
            model=model,
            cost_usd=cost_usd,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_status=cost_status,
        )
        self._session.add(entry)
        await self._session.flush()  # populate id/created_at without commit
        return entry

    async def sum_for_task(self, task_id: str) -> Decimal:
        """Total cost charged against one task_id — drives per_task budget cap.

        Includes every row regardless of `cost_status`. For strict budget
        accounting (where you only trust prices that were actually computed)
        use `sum_recorded_for_task` instead.
        """
        stmt = select(func.coalesce(func.sum(CostMeter.cost_usd), 0)).where(
            CostMeter.task_id == task_id
        )
        result = await self._session.execute(stmt)
        return Decimal(str(result.scalar_one()))

    async def sum_recorded_for_task(self, task_id: str) -> Decimal:
        """Total cost from rows with `cost_status == RECORDED`.

        BudgetEnforcement should call this — degraded rows (pricing failed,
        usage missing) have unknown cost and including them would either
        let real spend exceed the cap (treating unknown as 0) or block
        all calls (treating unknown as infinity). Neither is acceptable,
        so we report only what we know.
        """
        stmt = select(func.coalesce(func.sum(CostMeter.cost_usd), 0)).where(
            CostMeter.task_id == task_id,
            CostMeter.cost_status == CostStatus.RECORDED,
        )
        result = await self._session.execute(stmt)
        return Decimal(str(result.scalar_one()))

    async def sum_for_repo_since(self, repo: str, since: datetime) -> Decimal:
        """Total cost for a repo since `since` — drives monthly_soft_cap."""
        stmt = select(func.coalesce(func.sum(CostMeter.cost_usd), 0)).where(
            CostMeter.repo == repo,
            CostMeter.created_at >= since,
        )
        result = await self._session.execute(stmt)
        return Decimal(result.scalar_one())

    async def sum_global_since(self, since: datetime) -> Decimal:
        """Total cost across all repos — drives global_hard_kill."""
        stmt = select(func.coalesce(func.sum(CostMeter.cost_usd), 0)).where(
            CostMeter.created_at >= since,
        )
        result = await self._session.execute(stmt)
        return Decimal(result.scalar_one())


class AuditLogRepo:
    """Reads + writes for the audit_log table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def write(
        self,
        *,
        phase: str,
        delivery_id: str | None = None,
        repo: str | None = None,
        actor: str | None = None,
        workflow: str | None = None,
        outcome: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Append an audit entry. Caller commits the session."""
        entry = AuditLog(
            phase=phase,
            delivery_id=delivery_id,
            repo=repo,
            actor=actor,
            workflow=workflow,
            outcome=outcome,
            details=details,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def by_delivery(self, delivery_id: str) -> Sequence[AuditLog]:
        """All entries for one webhook delivery — useful for debugging."""
        stmt = (
            select(AuditLog)
            .where(AuditLog.delivery_id == delivery_id)
            .order_by(AuditLog.created_at.asc())
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def by_id(self, audit_id: UUID) -> AuditLog | None:
        return await self._session.get(AuditLog, audit_id)


# Convenience helper used by several callers to compute the "this month
# so far" window. PRD §4.5 monthly cap is calendar-month resetting, but
# v0.1 uses a rolling 30-day window — simpler and good enough.
def rolling_month_window(now: datetime) -> datetime:
    return now - timedelta(days=30)
