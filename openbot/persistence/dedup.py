"""Webhook delivery dedup — Redis-backed, fall-open.

PRD §5.1: dedupe by `X-GitHub-Delivery` so GitHub's retry (slow handler,
non-2xx response, network blip) doesn't replay a workflow twice. Two
ACK comments for one issue, two `cost_meter` rows for one LLM call, two
`add_label` round-trips — all real costs we'd rather not pay.

Implementation:
  - Redis `SET key value NX EX <ttl>` — one round-trip, atomic check-and-mark.
  - TTL = 10 minutes (GitHub's retry storm finishes well inside that;
    realistic duplicate-within-seconds is what we protect against).
  - Key shape `openbot:dedup:webhook:<channel>:<delivery_id>` so channels
    are isolated (a GitHub delivery_id can never collide with a Linear one).

Failure mode is **fail-open**: if Redis is down or not configured, the
dedup returns "fresh" and logs a WARNING. The reasoning:

  - Drop-on-error would block every webhook the moment Redis flaps. Bad.
  - Replay-on-error is "double-process a few webhooks" — recoverable,
    and the duplication shows up loudly in audit logs once Redis returns.

This trade-off becomes safer once PR 16 lands `cost_meter` + per-task budget
caps: even a double-run can't burn more than the cap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

import redis.asyncio as redis_async

_logger = logging.getLogger(__name__)

_DEDUP_TTL_SECONDS: Final = 10 * 60
_KEY_PREFIX: Final = "openbot:dedup:webhook"


@dataclass(frozen=True, slots=True)
class DedupResult:
    """Outcome of a single check_and_mark call.

    Truthiness == `fresh`; lets the caller write `if await dedup.check_and_mark(...)`.
    """

    fresh: bool
    """True when this delivery_id had not been seen → process it.
    False when a previous webhook already marked the key → drop."""

    fallback_open: bool = False
    """True iff Redis was unreachable / unconfigured and we fell open.
    Caller can audit-log this so it's visible when dedup is silently disabled."""

    def __bool__(self) -> bool:
        return self.fresh


class WebhookDedup:
    """Atomic delivery dedup against a Redis backend.

    `redis` can be None (no Redis configured) — every call then returns
    fresh=True, fallback_open=True. Useful for `make dev` without redis-server.
    """

    def __init__(
        self,
        redis: redis_async.Redis | None,
        *,
        ttl_seconds: int = _DEDUP_TTL_SECONDS,
    ) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def check_and_mark(self, channel: str, delivery_id: str) -> DedupResult:
        """Check whether (channel, delivery_id) was seen and atomically mark it.

        Args:
            channel:     channel name, e.g. "github". Used as namespace.
            delivery_id: webhook unique id (X-GitHub-Delivery). Empty string is
                         treated as fresh — missing id is a parsing concern,
                         not a dedup concern.
        """
        if not delivery_id:
            return DedupResult(fresh=True)

        if self._redis is None:
            _logger.warning(
                "dedup_skipped_no_redis",
                extra={"channel": channel, "delivery_id": delivery_id},
            )
            return DedupResult(fresh=True, fallback_open=True)

        key = f"{_KEY_PREFIX}:{channel}:{delivery_id}"
        try:
            # SET key "1" NX EX <ttl> — one atomic operation:
            #   NX:  only set if absent (= we are the first)
            #   EX:  expire after TTL seconds so memory is bounded
            # Returns True when newly set; None/False when already existed.
            was_set = await self._redis.set(key, "1", nx=True, ex=self._ttl)
        except Exception:
            # Redis transient errors → fall open. Better to maybe-double-process
            # one webhook than to drop them all when Redis is flapping.
            _logger.exception(
                "dedup_redis_error_fail_open",
                extra={"channel": channel, "delivery_id": delivery_id},
            )
            return DedupResult(fresh=True, fallback_open=True)

        return DedupResult(fresh=bool(was_set))
