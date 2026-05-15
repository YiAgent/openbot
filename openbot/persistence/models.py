"""SQLAlchemy 2.x models — Postgres in prod, SQLite (aiosqlite) for unit tests.

Tables live in this PR (slice 2/3):
  cost_meter   — one row per LiteLLM call (PRD §4.5 budget enforcement source)
  audit_log    — one row per workflow lifecycle event (PRD §9.4 audit trail)

Tables landing in slice 3/3 (middleware):
  rate_limit_audit  — Redis is the hot path, this is the daily rollup

Type choices are intentionally cross-dialect (no JSONB / PG-only types) so
unit tests run against aiosqlite without a docker daemon. Production gets the
same SQL because SQLAlchemy maps `JSON` → `JSONB` on PostgreSQL automatically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Index, Integer, Numeric, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    """Server-side timestamp default. `datetime.now(UTC)` is tz-aware."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Shared declarative base. Every model inherits from this."""


class CostMeter(Base):
    """One row per LiteLLM call (PRD §4.5).

    Read by:
      - BudgetEnforcement middleware (PR 17) — sums per-task / per-month
      - `openbot audit` CLI (v0.2)           — usage reports

    Written by:
      - `openbot.llm.complete()` after every successful LLM call
    """

    __tablename__ = "cost_meter"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)

    # Scope keys — what task did this charge belong to?
    repo: Mapped[str] = mapped_column(String(255))
    feature: Mapped[str] = mapped_column(String(16))  # triage / review / fix / chat
    task_id: Mapped[str] = mapped_column(String(64))  # delivery_id or composite

    # The actual call
    model: Mapped[str] = mapped_column(String(64))  # e.g. anthropic/claude-opus-4-7
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 8))  # support µ-USD granularity

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_cost_meter_repo_created_at", "repo", "created_at"),
        Index("ix_cost_meter_task_id", "task_id"),
        Index("ix_cost_meter_created_at", "created_at"),
    )


class AuditLog(Base):
    """Workflow lifecycle audit trail (PRD §9.4).

    One row per phase: `started`, `completed`, `failed`, `skipped`, or
    `rejected` (signature failures). PII-aware: do NOT store body text,
    tokens, or anything that could leak secrets. `details` is JSON for
    structured fields the consumer chooses.
    """

    __tablename__ = "audit_log"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)

    delivery_id: Mapped[str | None] = mapped_column(String(64))
    repo: Mapped[str | None] = mapped_column(String(255))
    actor: Mapped[str | None] = mapped_column(String(64))
    workflow: Mapped[str | None] = mapped_column(String(16))  # triage/review/fix/chat
    phase: Mapped[str] = mapped_column(String(16))  # started/completed/failed/skipped/rejected
    outcome: Mapped[str | None] = mapped_column(String(255))  # one-line summary, never the body
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_audit_log_delivery_id", "delivery_id"),
        Index("ix_audit_log_repo_created_at", "repo", "created_at"),
        Index("ix_audit_log_phase_created_at", "phase", "created_at"),
    )
