"""Persistence layer — Redis + Postgres.

Redis (slice 1/3):
  - webhook dedup        (openbot.infrastructure.persistence.dedup)
  - rate-limit counters  (middleware slice)
  - workflow queue       (middleware slice)

Postgres (slice 2/3, this slice):
  - cost_meter           (every LLM call, drives PRD §4.5 budget caps)
  - audit_log            (workflow lifecycle, drives PRD §9.4 audit trail)
"""

from openbot.infrastructure.persistence.db import (
    create_schema,
    make_engine,
    make_session_factory,
    session_scope,
)
from openbot.infrastructure.persistence.dedup import DedupOutcome, WebhookDedup
from openbot.infrastructure.persistence.models import (
    AuditLog,
    Base,
    CostMeter,
    CostStatus,
    Intent,
    State,
    TaskRun,
    Workflow,
    WorkflowPhase,
)
from openbot.infrastructure.persistence.redis import make_client
from openbot.infrastructure.persistence.repository import (
    AuditLogRepo,
    CostMeterRepo,
    rolling_month_window,
)

__all__ = [
    "AuditLog",
    "AuditLogRepo",
    "Base",
    "CostMeter",
    "CostMeterRepo",
    "CostStatus",
    "DedupOutcome",
    "Intent",
    "State",
    "TaskRun",
    "WebhookDedup",
    "Workflow",
    "WorkflowPhase",
    "create_schema",
    "make_client",
    "make_engine",
    "make_session_factory",
    "rolling_month_window",
    "session_scope",
]
