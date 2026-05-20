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
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
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

# Re-export the state-machine enums so callers can write
# ``from openbot.infrastructure.persistence.models import State, Intent`` and avoid the
# import-cycle dance of pulling from ``openbot.application.state`` (which imports
# models via runs_repo). The enums themselves live in
# ``openbot.domain.intents`` — this is just an alias for ergonomics.
from openbot.domain.intents import Intent, State

# Re-exported from openbot.domain.workflows — kept for backward compat
from openbot.domain.workflows import Feature, Workflow, WorkflowPhase

__all__ = [
    "AuditLog",
    "Base",
    "CostMeter",
    "CostStatus",
    "Feature",
    "Intent",
    "State",
    "TaskRun",
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


class Base(DeclarativeBase):
    """Shared declarative base. Every model inherits from this."""


class CostMeter(Base):
    """One row per LiteLLM call (PRD §4.5).

    Read by:
      - BudgetEnforcement middleware — sums per-task / per-month
      - `openbot audit` CLI (v0.2) — usage reports

    Written by:
      - `openbot.infrastructure.llm.complete()` after every successful LLM call

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


class TaskRun(Base):
    """Per-resource state row driving the input-side state machine.

    One row per ``resource_key`` (e.g. ``github:owner/repo:pr:42``). The
    classifier reads this row to decide START / SUPERSEDE / CANCEL /
    IGNORE; the receive side CAS-writes a new version after enqueue.

    ``row_version`` is the SQLAlchemy ``version_id_col`` — every UPDATE
    increments it and refuses the write if another transaction beat us
    between SELECT and UPDATE. Coupled with the per-resource Redis lock
    in ``state.resource_lock`` this gives belt-and-suspenders mutual
    exclusion: the lock prevents the contention in practice, the version
    column catches the rare lock-miss (lock TTL expiry mid-transaction,
    redis flap) instead of silently double-writing.

    ``last_event_seq`` is the monotonic high-water mark — for GitHub we
    use the ``updated_at`` epoch-ms of ``issue`` / ``pull_request``.
    Events with ``event.event_seq < last_event_seq`` are out-of-order
    arrivals; the classifier's caller drops them with
    ``Intent.IGNORE`` + ``reason="stale_event"``.

    The row is intentionally a single source of truth for the state
    transition trace: history rows live in ``audit_log.details`` JSON
    rather than a separate table — keeps the schema flat for v0.1.
    """

    __tablename__ = "task_runs"

    # Composite resource id (channel:repo:type:n). 320 chars supports
    # full GitHub `owner/repo` (max 140) + the longest reasonable issue
    # number (10 digits) + namespace prefix with comfortable headroom.
    resource_key: Mapped[str] = mapped_column(String(320), primary_key=True)

    state: Mapped[State] = mapped_column(
        Enum(State, native_enum=False, length=16, validate_strings=True),
        default=State.IDLE,
    )

    # Active run identifier. NULL when state is IDLE/CLOSED. 64 chars to
    # match cost_meter.task_id, since the worker still threads run_id
    # through to cost_meter rows during the v0.1 transition.
    current_run_id: Mapped[str | None] = mapped_column(String(64), default=None)

    # Monotonic per-resource sequence — epoch-ms of the most recent
    # event we've applied. ``BigInteger`` is required: ``Integer`` (32-bit
    # signed) tops out in 2038 and SQLite happily silently truncates.
    last_event_seq: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    last_intent: Mapped[Intent | None] = mapped_column(
        Enum(Intent, native_enum=False, length=16, validate_strings=True),
        default=None,
    )

    # GitHub X-GitHub-Delivery of the most recent applied event — for
    # debugging "which delivery moved this row".
    last_delivery_id: Mapped[str | None] = mapped_column(String(64), default=None)

    # SHA of the PR head commit at the end of the most recent successful
    # REVIEW run. NULL until the first review completes. The dispatcher
    # reads this to compute DiffScope.is_incremental for PR_SYNCHRONIZED
    # events — if the incoming `before` SHA matches this, the push is
    # incremental (only the diff since last review needs re-checking).
    last_reviewed_sha: Mapped[str | None] = mapped_column(String(40), default=None)

    # SQLAlchemy version_id_col. Incremented atomically on every UPDATE
    # that goes through the ORM mapper; mismatched version raises
    # StaleDataError, which ``runs_repo.transition`` translates into a
    # bounded CAS retry.
    row_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # ``__mapper_args__`` is SQLAlchemy declarative metadata, not instance
    # state; the ClassVar annotation tells ruff (and any static reader)
    # that the dict is intentionally shared across the class.
    __mapper_args__: ClassVar[dict[str, Any]] = {
        "version_id_col": row_version,
        # We update ``row_version`` ourselves inside the transition helper
        # rather than letting the mapper auto-increment; cleaner semantics
        # when the helper is the only writer of this table.
        "version_id_generator": False,
    }

    __table_args__ = (Index("ix_task_runs_state_updated_at", "state", "updated_at"),)
