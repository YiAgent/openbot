"""Enqueue side of the workflow queue.

The webhook handler calls ``enqueue(redis, payload)`` after dedup + router
dispatch. We send a single field ``"json"`` whose value is the entire
serialized payload — this keeps the redis-cli view legible (one row,
one JSON blob) and avoids type-coercion surprises (Redis Streams store
field values as strings regardless of how you XADD them).

Bounded stream (``MAXLEN ~ MAX_STREAM_LEN``):

  - ``MAXLEN ~`` (approximate) is cheaper than the exact form and is
    fine for capacity protection. The tilde lets Redis pick a length
    near our target rather than scanning all entries each XADD.
  - Once the cap is reached, the oldest entries are evicted. This is
    a v0.1 trade-off: at production scale we'd want a watcher that
    alerts when MAXLEN truncation actually happens. For an individual
    maintainer's instance, MAX_STREAM_LEN=10_000 is months of headroom.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openbot.infrastructure.queue.payload import MAX_STREAM_LEN, STREAM_NAME, QueuePayload

if TYPE_CHECKING:
    import redis.asyncio as redis_async

    from openbot.domain.events import UnifiedEvent
    from openbot.domain.workflows import Feature

_logger = logging.getLogger(__name__)


async def enqueue(redis: redis_async.Redis, payload: QueuePayload) -> str:
    """XADD the payload to the work stream.

    Returns the entry ID Redis assigns (``"<ms>-<seq>"``). Caller logs
    it so a webhook → entry-id correspondence shows up in audit.

    Raises whatever the Redis client raises — the webhook handler
    catches and degrades to in-process BackgroundTask fallback.
    """
    entry_id = await redis.xadd(
        STREAM_NAME,
        {"json": payload.to_json()},
        maxlen=MAX_STREAM_LEN,
        approximate=True,
    )
    # Some Redis clients return bytes, others str. Normalize so callers
    # can log it uniformly.
    if isinstance(entry_id, bytes | bytearray):
        entry_id = entry_id.decode("ascii", errors="replace")
    _logger.info(
        "workflow_enqueued",
        extra={
            "delivery_id": payload.delivery_id,
            "repo": payload.repo,
            "feature": payload.feature,
            "task_id": payload.task_id,
            "entry_id": entry_id,
        },
    )
    return entry_id


class RedisStreamQueue:
    """Stateful QueuePort impl — owns one Redis client."""

    def __init__(self, redis: redis_async.Redis) -> None:
        self._redis = redis

    async def enqueue(
        self,
        event: UnifiedEvent,
        *,
        feature: Feature,
        task_id: str,
        check_run_id: int | None = None,
        intent: str | None = None,
        run_id: str | None = None,
        prev_run_id: str | None = None,
        resource_key: str | None = None,
        event_seq: int = 0,
    ) -> str:
        """Build QueuePayload from logical params and XADD to the stream."""
        payload = QueuePayload.from_event(
            event,
            feature=feature,
            task_id=task_id,
            check_run_id=check_run_id,
            intent=intent,
            run_id=run_id,
            prev_run_id=prev_run_id,
            resource_key=resource_key,
            event_seq=event_seq,
        )
        return await enqueue(self._redis, payload)


if TYPE_CHECKING:
    from openbot.application.ports.queue import QueuePort

    _witness: QueuePort = RedisStreamQueue(redis=None)  # type: ignore[arg-type]
