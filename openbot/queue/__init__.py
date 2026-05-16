"""Redis Stream workflow queue — harness spec §3 M10.

Replaces FastAPI ``BackgroundTasks`` with a persistent queue so workflows
survive a worker restart. The webhook handler enqueues; one or more
worker processes (``python -m openbot.queue.runner``) consume.

Slice D delivers the **single-process** variant per spec §9.3:
``asyncio.gather`` over N consumers in one process, default N=4
(``OPENBOT_WORKER_CONCURRENCY``). Multi-process is a v0.2 extension —
the consumer-group plumbing is already in place; running multiple
``runner.py`` processes that share the same group name is the only
configuration change needed.

Stream layout
-------------

  ``openbot:workflows``               main work stream (capped MAXLEN)
  ``openbot:workflows:group``         consumer group name
  ``openbot:workflows:dead``          DLQ — entries that exceeded retry cap
  ``openbot:workflows:retries:<id>``  per-entry attempt counter, 7d TTL
"""

from openbot.queue.enqueue import enqueue
from openbot.queue.payload import (
    DEAD_STREAM,
    GROUP_NAME,
    MAX_STREAM_LEN,
    STREAM_NAME,
    QueuePayload,
    deserialize_payload,
)
from openbot.queue.worker import consume_loop, ensure_consumer_group

__all__ = [
    "DEAD_STREAM",
    "GROUP_NAME",
    "MAX_STREAM_LEN",
    "STREAM_NAME",
    "QueuePayload",
    "consume_loop",
    "deserialize_payload",
    "enqueue",
    "ensure_consumer_group",
]
