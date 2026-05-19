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

Failure mode is **fall-open**: if Redis is down or not configured, the
outcome is `FALLBACK_OPEN`. The caller is expected to process the event
anyway, but the audit log records that dedup couldn't be enforced.

  - Drop-on-error would block every webhook the moment Redis flaps. Bad.
  - Replay-on-error is "double-process a few webhooks" — recoverable,
    and the duplication shows up loudly in audit logs once Redis returns.

The three outcomes (`FRESH`, `DUPLICATE`, `FALLBACK_OPEN`) are an enum
rather than a `(bool, bool)` pair so call sites cannot accidentally drop
the `FALLBACK_OPEN` audit signal via a truthy shortcut.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

import redis.asyncio as redis_async

# Re-exported from openbot.domain.dedup — kept for backward compat.
from openbot.domain.dedup import DedupOutcome

_logger = logging.getLogger(__name__)

_DEDUP_TTL_SECONDS: Final = 10 * 60
_KEY_PREFIX: Final = "openbot:dedup:webhook"


class WebhookDedup:
    """Atomic delivery dedup against a Redis backend.

    `redis` can be None (no Redis configured) — every call then returns
    `DedupOutcome.FALLBACK_OPEN`. Useful for `make dev` without redis-server.
    """

    def __init__(
        self,
        redis: redis_async.Redis | None,
        *,
        ttl_seconds: int = _DEDUP_TTL_SECONDS,
    ) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def check_and_mark(self, channel: str, delivery_id: str) -> DedupOutcome:
        """Check whether (channel, delivery_id) was seen and atomically mark it.

        Args:
            channel:     channel name, e.g. "github". Used as namespace.
            delivery_id: webhook unique id (X-GitHub-Delivery). Empty string is
                         treated as FRESH — missing id is a parsing concern,
                         not a dedup concern.
        """
        if not delivery_id:
            return DedupOutcome.FRESH

        if self._redis is None:
            _logger.warning(
                "dedup_skipped_no_redis",
                extra={"channel": channel, "delivery_id": delivery_id},
            )
            return DedupOutcome.FALLBACK_OPEN

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
            return DedupOutcome.FALLBACK_OPEN

        return DedupOutcome.FRESH if was_set else DedupOutcome.DUPLICATE


if TYPE_CHECKING:
    from openbot.application.ports.dedup import DedupPort

    # Structural check — fails type-checking if the API drifts.
    _witness: DedupPort = WebhookDedup(redis=None)  # type: ignore[arg-type]
