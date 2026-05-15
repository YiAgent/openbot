"""SQLAlchemy 2.x models — Postgres in prod, SQLite (aiosqlite) for unit tests.

Tables live in this slice (slice 2/3):
  cost_meter   — one row per LiteLLM call (PRD §4.5 budget enforcement source)
  audit_log    — one row per workflow lifecycle event (PRD §9.4 audit trail)

Tables landing in the middleware slice:
  rate_limit_audit  — Redis is the hot path, this is the daily rollup

Type choices are intentionally cross-dialect (no JSONB / PG-only types) so
unit tests run against aiosqlite without a docker daemon. Production gets the
same SQL because SQLAlchemy maps `JSON` → `JSONB` on PostgreSQL automatically.

We do NOT use `Enum(...)` with `native_enum=True` for the same reason — SQLite
has no native enum type. `String` columns coupled with Python-side StrEnums
(`Feature`, `Workflow`, `WorkflowPhase`, `CostStatus`) give us cross-dialect
SQL + type safety in the application layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Re-export Feature so models can reference the enum without a circular
# import via openbot.llm. The Feature StrEnum lives in openbot.llm.router.
from openbot.llm.router import Feature

__all__ = [
    "AuditLog",
    "Base",
    "CostMeter",
    "CostStatus",
    "Feature",
    "Workflow",
    "WorkflowPhase",
]


def _utcnow() -> datetime:
    """Server-side timestamp default. `datetime.now(UTC)` is tz-aware."""
    return datetime.now(UTC)


class CostStatus(StrEnum):
    """How a `cost_meter` row was produced. Lets BudgetEnforcement distinguish
    "real $0 call" from "we couldn't price this" — without this, every zero
    row looks the same and budget caps go blind.
    """

    RECORDED = "recorded"
    """Cost was computed from LiteLLM normally; the row is authoritative."""

    PRICED_ZERO = "priced_zero"
    """LiteLLM returned `None` cost (unknown model pricing). Tokens are real;
    cost is unknown, not zero."""

    PRICING_FAILED = "pricing_failed"
    """`litellm.completion_cost` raised. Tokens are real; cost is unknown."""

    USAGE_MISSING = "usage_missing"
    """`response.usage` was absent from the LiteLLM response. Tokens AND cost
    are unknown; this row carries the call's existence, not its size."""

    EXTRACTION_FAILED = "extraction_failed"
    """`_extract_content` raised AFTER the upstream vendor already billed.
    Cost was best-effort computed; the call's result is not in `content`."""


class Workflow(StrEnum):
    """Workflow identity for `audit_log.workflow` and joined queries."""

    TRIAGE = "triage"
    REVIEW = "review"
    FIX = "fix"
    CHAT = "chat"


class WorkflowPhase(StrEnum):
    """Lifecycle position for one workflow run."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"  # signature mismatch / unauthorized


class Base(DeclarativeBase):
    """Shared declarative base. Every model inherits from this."""


class CostMeter(Base):
    """One row per LiteLLM call (PRD §4.5).

    Read by:
      - BudgetEnforcement middleware — sums per-task / per-month
      - `openbot audit` CLI (v0.2) — usage reports

    Written by:
      - `openbot.llm.complete()` after every successful LLM call

    `cost_status` tells the reader why `cost_usd` might be 0:
    a real cheap call (`RECORDED`) vs pricing-data-missing (`PRICED_ZERO`)
    vs pricer-bug (`PRICING_FAILED`) vs no-usage (`USAGE_MISSING`).
    BudgetEnforcement should only trust `RECORDED` for hard cap math.
    """

    __tablename__ = "cost_meter"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)

    # Scope keys — what task did this charge belong to?
    # 255 = comfortable upper bound for GitHub owner(39) + '/' + name(100).
    repo: Mapped[str] = mapped_column(String(255))
    feature: Mapped[Feature] = mapped_column(
        # native_enum=False keeps SQLite happy; values stored as plain strings.
        Enum(Feature, native_enum=False, length=16, validate_strings=True)
    )
    # 64 chars covers GitHub delivery_id (UUID, 36 chars) + future composite keys.
    task_id: Mapped[str] = mapped_column(String(64))

    # The actual call
    model: Mapped[str] = mapped_column(String(64))  # e.g. anthropic/claude-opus-4-7
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Numeric(12, 6) — 6 dp matches LiteLLM's pricing granularity; 12 total
    # supports six-figure cumulative spend without overflow.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    cost_status: Mapped[CostStatus] = mapped_column(
        Enum(CostStatus, native_enum=False, length=24, validate_strings=True),
        default=CostStatus.RECORDED,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_cost_meter_repo_created_at", "repo", "created_at"),
        Index("ix_cost_meter_task_id", "task_id"),
        Index("ix_cost_meter_created_at", "created_at"),
        # CHECK constraints are advisory in SQLite but enforced in Postgres.
        # Negative cost / tokens are bugs; surface them at the DB layer.
        CheckConstraint("cost_usd >= 0", name="ck_cost_meter_cost_nonneg"),
        CheckConstraint("prompt_tokens >= 0", name="ck_cost_meter_prompt_nonneg"),
        CheckConstraint("completion_tokens >= 0", name="ck_cost_meter_completion_nonneg"),
    )


class AuditLog(Base):
    """Workflow lifecycle audit trail (PRD §9.4).

    One row per phase transition. PII-aware: do NOT store body text, tokens,
    or anything that could leak secrets. `details` is JSON for structured
    fields the caller chooses.
    """

    __tablename__ = "audit_log"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)

    delivery_id: Mapped[str | None] = mapped_column(String(64))
    repo: Mapped[str | None] = mapped_column(String(255))
    actor: Mapped[str | None] = mapped_column(String(64))
    workflow: Mapped[Workflow | None] = mapped_column(
        Enum(Workflow, native_enum=False, length=16, validate_strings=True)
    )
    phase: Mapped[WorkflowPhase] = mapped_column(
        Enum(WorkflowPhase, native_enum=False, length=16, validate_strings=True)
    )
    outcome: Mapped[str | None] = mapped_column(String(255))  # one-line summary, never the body
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_audit_log_delivery_id", "delivery_id"),
        Index("ix_audit_log_repo_created_at", "repo", "created_at"),
        Index("ix_audit_log_phase_created_at", "phase", "created_at"),
    )
