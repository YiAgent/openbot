"""Cooperative cancellation — in-process registry + cross-dyno Redis flag.

The state-machine receive side classifies a webhook as ``SUPERSEDE`` or
``CANCEL`` when a follow-up event should stop a still-running worker
for the same resource. The worker that picks up the new entry is
responsible for actually stopping the old run; this module gives it
two cooperating mechanisms:

1. **In-process registry** — ``RUNNING: dict[run_id, asyncio.Task]``.
   When the worker starts a handler it registers its own ``asyncio.Task``
   under the run_id. ``signal(run_id)`` calls ``.cancel()`` if the task
   lives in the local process. Cheap; immediate; only works when the
   superseding event happens to be processed by the same worker
   process that's running the prior task.

2. **Cross-dyno Redis flag** — ``openbot:run_cancel:{run_id}``. Set
   alongside the in-process cancel so a worker on dyno B can stop a run
   that's executing on dyno A. The handler polls ``checkpoint(run_id)``
   at well-known yield points (between middleware steps, between LLM
   calls); ``checkpoint`` raises ``RunCancelledError`` (a subclass of
   ``asyncio.CancelledError``) when the flag is set.

The two-layer design is deliberate: the in-process registry catches
the common case fast, the Redis flag catches the cross-dyno case at
the cost of poll latency. Pure-Redis would work but adds a network
round-trip to every cancel; pure-registry would silently fail when
SUPERSEDE crosses dyno boundaries.

Cooperative, not preemptive: ``asyncio.CancelledError`` is delivered at
the next ``await`` point. For our handlers that's fine — every LLM call,
DB write, and GitHub API call is an ``await``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import redis.asyncio as redis_async

_logger = logging.getLogger(__name__)

_FLAG_PREFIX: Final = "openbot:run_cancel"

# Flag TTL matches PRD §4.3 maximum agent budget (45 min) plus a safety
# margin. Run IDs are derived per-event in the receive side so the
# practical risk of "stale flag cancels a re-used run_id" is zero; the
# TTL is just memory hygiene for the long tail.
_FLAG_TTL_SECONDS: Final = 60 * 60


# In-process registry. One entry per locally-running handler. We
# deliberately use the module-level dict rather than a class instance
# so any code path in this Python process can ``signal`` without
# threading a registry handle through every call site.
_RUNNING: dict[str, asyncio.Task[object]] = {}


def _flag_key(run_id: str) -> str:
    return f"{_FLAG_PREFIX}:{run_id}"


class RunCancelledError(asyncio.CancelledError):
    """Raised by ``checkpoint`` when the cross-dyno cancel flag is set.

    Subclasses ``asyncio.CancelledError`` so existing
    ``except CancelledError`` blocks in handler code still catch us —
    we don't want to force every middleware author to swap exception
    types when this lands.

    The ``reason`` attribute is set so logs can distinguish "cancelled
    by user @cancel" (handled here) from a plain task cancel.
    """

    def __init__(self, reason: str = "superseded_or_cancelled") -> None:
        super().__init__(reason)
        self.reason = reason


def register(run_id: str, task: asyncio.Task[object] | None = None) -> None:
    """Register the current task under ``run_id`` so ``signal`` can find it.

    Pass ``task`` explicitly when you need to register a child task; the
    default uses ``asyncio.current_task()`` which is what handlers want.

    Re-registering an existing ``run_id`` overwrites the entry — useful
    if a SUPERSEDE arrives mid-flight and the worker starts a fresh
    asyncio task under the same id. The previous task should already be
    cancelled by then via ``signal``.
    """
    actual = task or asyncio.current_task()
    if actual is None:
        # Defensive: called outside an asyncio context. Shouldn't happen
        # from a worker but a unit test might exercise this directly.
        _logger.warning("cancellation_register_no_current_task", extra={"run_id": run_id})
        return
    _RUNNING[run_id] = actual


def deregister(run_id: str) -> None:
    """Remove the registry entry. Idempotent — missing ids are a no-op.

    Always called in a ``finally`` block so a handler exception path
    doesn't leak a stale entry that would later receive a spurious
    cancel intended for a future run with the same id.
    """
    _RUNNING.pop(run_id, None)


async def signal(redis: redis_async.Redis | None, run_id: str) -> None:
    """Mark ``run_id`` as cancelled — in-process AND via Redis flag.

    The worker calls this on dequeue when the intent is ``SUPERSEDE`` or
    ``CANCEL`` and a ``prev_run_id`` is present. We always set the Redis
    flag first (cheap, durable) and THEN call ``.cancel()`` on any local
    task — this ordering means a handler that wakes from its current
    ``await`` sees the flag at the next ``checkpoint`` even if the local
    cancel arrives a moment later.

    ``redis=None`` is supported so unit tests can exercise the
    in-process path without fakeredis. The cross-dyno path is then a
    no-op which is documented at the call site.
    """
    if redis is not None:
        try:
            await redis.set(_flag_key(run_id), "1", ex=_FLAG_TTL_SECONDS)
        except Exception:
            # A failed flag write is non-fatal — we'll still cancel the
            # local task. The cross-dyno case degrades to "no cancel"
            # which is acceptable while Redis is flapping.
            _logger.exception("cancellation_flag_write_failed", extra={"run_id": run_id})

    task = _RUNNING.get(run_id)
    if task is not None and not task.done():
        task.cancel()


async def is_cancelled(redis: redis_async.Redis | None, run_id: str) -> bool:
    """Cheap point query — has ``run_id`` been signalled?

    Used by ``checkpoint``; exported separately so handlers that want
    to inspect the flag without raising (e.g. for an "abort early"
    optimization) can still do so.
    """
    if redis is None:
        return False
    try:
        return await redis.exists(_flag_key(run_id)) == 1
    except Exception:
        # Redis flap — fall closed (treat as "not cancelled") so a
        # flapping flag check doesn't spuriously abort a healthy run.
        _logger.exception("cancellation_flag_read_failed", extra={"run_id": run_id})
        return False


async def checkpoint(redis: redis_async.Redis | None, run_id: str) -> None:
    """Raise ``RunCancelledError`` if ``run_id`` has been signalled.

    Call at well-known yield points inside a handler — between
    middleware steps, between LLM calls, before writing back to GitHub.
    Cooperative: if the handler never hits a checkpoint, it runs to
    completion (the worker's ``asyncio.Task.cancel()`` is still your
    other line of defence for in-process supersedes).
    """
    if await is_cancelled(redis, run_id):
        raise RunCancelledError()


def clear_for_tests() -> None:
    """Test-only: wipe the in-process registry.

    Pytest fixtures call this between cases so a leaked register from
    one test can't haunt the next. Not part of the production surface
    — production processes the registry through the worker's
    ``try/finally`` discipline.
    """
    _RUNNING.clear()


__all__ = [
    "RunCancelledError",
    "checkpoint",
    "clear_for_tests",
    "deregister",
    "is_cancelled",
    "register",
    "signal",
]
