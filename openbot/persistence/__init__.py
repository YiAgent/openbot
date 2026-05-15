"""Persistence layer — Redis (this PR) + Postgres (PR 16).

v0.1 uses Redis for three concerns:
  - webhook dedup        (this PR, openbot.persistence.dedup)
  - rate-limit counters  (PR 17)
  - workflow queue       (PR 16)

Postgres lands in PR 16 for `cost_meter` / `audit_log` / `rate_limit_audit`.
"""

from openbot.persistence.dedup import DedupResult, WebhookDedup
from openbot.persistence.redis import make_client

__all__ = ["DedupResult", "WebhookDedup", "make_client"]
