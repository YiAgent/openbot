"""Persistence layer — Redis + (later) Postgres.

v0.1 uses Redis for three concerns:
  - webhook dedup        (this slice; `openbot.persistence.dedup`)
  - rate-limit counters  (later slice — middleware)
  - workflow queue       (later slice — middleware)

Postgres lands in a later slice for `cost_meter` / `audit_log` / `rate_limit_audit`.
"""

from openbot.persistence.dedup import DedupOutcome, WebhookDedup
from openbot.persistence.redis import make_client

__all__ = ["DedupOutcome", "WebhookDedup", "make_client"]
