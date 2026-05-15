"""Persistence layer — Redis + Postgres.

Redis (slice 1/3):
  - webhook dedup        (openbot.persistence.dedup)
  - rate-limit counters  (middleware slice)
  - workflow queue       (middleware slice)

Postgres (slice 2/3, this slice):
  - cost_meter           (every LLM call, drives PRD §4.5 budget caps)
  - audit_log            (workflow lifecycle, drives PRD §9.4 audit trail)
"""

from openbot.persistence.db import (
    create_schema,
    make_engine,
    make_session_factory,
    session_scope,
)
from openbot.persistence.dedup import DedupOutcome, WebhookDedup
from openbot.persistence.models import AuditLog, Base, CostMeter
from openbot.persistence.redis import make_client
from openbot.persistence.repository import (
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
    "DedupOutcome",
    "WebhookDedup",
    "create_schema",
    "make_client",
    "make_engine",
    "make_session_factory",
    "rolling_month_window",
    "session_scope",
]
