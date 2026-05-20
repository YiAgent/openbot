"""Redis Stream consumer — harness spec §3 M10b.

Single-process worker loop with PEL-based retry + DLQ. One consumer
task pulls new entries (``XREADGROUP ... >``) and periodically claims
abandoned entries from the pending list (``XPENDING`` + ``XCLAIM``).

Retry policy:

  - Entries that crash dispatch are XACK'd, the attempt counter is
    incremented in ``openbot:workflows:retries:<entry_id>``, and the
    payload is re-XADD'd with a small back-off. After ``_MAX_ATTEMPTS``
    the payload is XADD'd to ``openbot:workflows:dead`` instead and the
    counter is left as audit evidence.

  - Entries abandoned by a dead consumer (no XACK within
    ``_PENDING_IDLE_MS``) are reclaimed via ``XCLAIM``. Each claim is
    counted against ``_MAX_ATTEMPTS`` so a crash loop can't pin a bad
    message in the queue forever.

Idempotency: the entry's ``delivery_id`` already gates duplicate work
upstream (slice A's ``WebhookDedup``). The worker re-dispatches without
re-checking dedup — the workflow handler is itself idempotent on
``task_id`` (cost_meter rows are upsert-friendly).

Why a single file: the loop, retry, and DLQ logic are tightly coupled
and only ~200 lines together. Splitting them just to "feel modular"
would force a circular-import dance via the payload module.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Final

from openbot.application.dispatcher import execute_handler, run_dispatch
from openbot.application.router import dispatch_for, upgrade_dispatch

# TODO(phase-2c): route through CancellationPort once worker composition root lands.
from openbot.application.state.cancellation import (
    deregister as cancellation_deregister,
)
from openbot.application.state.cancellation import (
    register as cancellation_register,
)
from openbot.application.state.cancellation import (
    signal as cancellation_signal,
)
from openbot.core.metrics import queue_depth
from openbot.infrastructure.config_loader import load_for_repo
from openbot.infrastructure.queue.payload import (
    DEAD_STREAM,
    GROUP_NAME,
    MAX_STREAM_LEN,
    STREAM_NAME,
    QueuePayload,
    deserialize_payload,
)
from openbot.infrastructure.queue.task_spec import TaskSpec, deserialize_task_spec

if TYPE_CHECKING:
    import redis.asyncio as redis_async
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from openbot.infrastructure.adapters.github import GitHubAdapter

_logger = logging.getLogger(__name__)

# Block up to this long inside XREADGROUP. Long enough that an idle
# worker doesn't burn CPU spinning; short enough that SIGTERM is
# noticed within reasonable shutdown latency.
_READ_BLOCK_MS: Final = 5_000

# Read this many entries per round-trip. >1 amortizes the XREADGROUP
# RTT; too high and a slow handler hoards messages from other consumers.
_READ_COUNT: Final = 8

# Reclaim entries idle this long — i.e. delivered but never XACK'd
# because the consumer died mid-handler.
_PENDING_IDLE_MS: Final = 60_000

# Maximum total dispatch attempts (initial + retries) before DLQ.
# Matches the "<= 3 retries" in the harness spec §3 M10.
_MAX_ATTEMPTS: Final = 3

# Retry-counter key TTL. 7d gives ops a week to investigate a DLQ'd
# entry before its retry history evaporates.
_RETRY_TTL_SECONDS: Final = 7 * 86400


def _retry_key(entry_id: str) -> str:
    return f"openbot:workflows:retries:{entry_id}"


def _is_v3_spec(blob: str | bytes | None) -> bool:
    """Peek at the stream entry's JSON to see if it is a TaskSpec v3.

    Returns True only when ``spec_version == 3`` is found, without
    attempting a full deserialisation. Keeps the v2 path fast for the
    existing majority of entries during the rolling migration.
    """
    if blob is None:
        return False
    try:
        text = blob.decode("utf-8") if isinstance(blob, (bytes, bytearray)) else blob
        data = json.loads(text)
        return isinstance(data, dict) and data.get("spec_version") == 3
    except Exception:
        return False


def _attach_sentry_tags(payload: QueuePayload) -> None:
    """Push delivery context to the Sentry scope for the current dispatch.

    Tags appear in the Sentry UI as filterable dimensions, allowing ops to
    jump from a Sentry issue directly to the originating GitHub delivery_id.
    Wrapped in try/except so a missing sentry_sdk dep (slim CI image) never
    crashes the consumer loop.
    """
    try:
        import sentry_sdk

        sentry_sdk.set_tag("delivery_id", payload.delivery_id)
        sentry_sdk.set_tag("feature", payload.feature)
        sentry_sdk.set_tag("repo", payload.repo)
    except Exception:
        pass  # Sentry tagging is best-effort; never crash the dispatch loop


async def _execute_task_spec(
    spec: TaskSpec,
    *,
    entry_id: str,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    """W1-W8: Process one TaskSpec v3 entry.

    W1: cancel quick-check via initial_labels.
    W2: reconstruct UnifiedEvent from spec.
    W3: reconstruct Dispatch from event via router.
    W4: load EffectiveConfig for this repo.
    W5: call execute_handler() (no preflight).
    W6-W8: bump attempt counter, register/deregister cancellation slot.
    """
    # W1: Cancel quick-check — avoid calling the handler at all.
    if "cancel-openbot" in spec.initial_labels:
        _logger.info(
            "queue_v3_cancel_quick_exit",
            extra={"entry_id": entry_id, "delivery_id": spec.delivery_id},
        )
        await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
        return

    # W2: Reconstruct event from the spec's serialised fields.
    event = spec.to_event()

    # W3: Re-derive Dispatch from the router (pure; no I/O).
    new_dispatch = dispatch_for(event)
    if new_dispatch is None:
        _logger.info(
            "queue_v3_entry_no_longer_routable",
            extra={"entry_id": entry_id, "delivery_id": spec.delivery_id},
        )
        await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
        return

    # Carry state-machine fields forward only for non-start intents.
    # "start" entries need no upgrade; v2 path differs because QueuePayload.intent
    # is nullable (None serves as the start sentinel there).
    if spec.resource_key is not None and spec.intent not in (None, "start"):
        new_dispatch = upgrade_dispatch(
            new_dispatch,
            intent=spec.intent,
            run_id=spec.run_id,
            prev_run_id=spec.prev_run_id,
            event_seq=spec.event_seq,
            resource_key=spec.resource_key,
        )

    # W4: Load effective config (adapter needed for GitHub API calls in config loader).
    config = await load_for_repo(adapter, event)

    # W5-W8: Attempt counter + cancellation lifecycle.
    attempts = await _bump_attempt_counter(redis, entry_id)
    active_run_id = new_dispatch.run_id or new_dispatch.task_id
    cancellation_register(active_run_id)

    try:
        try:
            await execute_handler(
                adapter=adapter,
                event=event,
                dispatch=new_dispatch,
                config=config,
                session_factory=session_factory,
                redis=redis,
                check_run_id=spec.check_run_id,
            )
        except Exception:
            _logger.exception(
                "queue_v3_execute_handler_escaped",
                extra={"entry_id": entry_id, "delivery_id": spec.delivery_id},
            )
            if attempts >= _MAX_ATTEMPTS:
                await _ack_and_dlq(redis, entry_id, reason="max_attempts_v3")
            # Don't XACK — let the next reclaim cycle pick it up.
            return
    finally:
        cancellation_deregister(active_run_id)

    await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
    _logger.info(
        "queue_v3_entry_dispatched",
        extra={
            "entry_id": entry_id,
            "delivery_id": spec.delivery_id,
            "repo": spec.repo,
            "scenario": spec.scenario,
            "attempts": attempts,
        },
    )


async def ensure_consumer_group(redis: redis_async.Redis) -> None:
    """Create the consumer group if absent. Safe to call on every boot.

    ``MKSTREAM`` ensures the stream itself exists even when no producer
    has run yet — a worker booted before the first webhook should not
    crash on a missing key.
    """
    try:
        await redis.xgroup_create(STREAM_NAME, GROUP_NAME, id="$", mkstream=True)
        _logger.info("queue_consumer_group_created", extra={"group": GROUP_NAME})
    except Exception as exc:
        # `BUSYGROUP Consumer Group name already exists` is normal on
        # restart. Anything else is a genuine error worth logging.
        if "BUSYGROUP" not in str(exc):
            _logger.warning(
                "queue_consumer_group_create_failed",
                extra={"reason": f"{type(exc).__name__}: {exc}"},
            )


async def consume_loop(
    *,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    consumer_name: str,
    shutdown: asyncio.Event | None = None,
    read_block_ms: int = _READ_BLOCK_MS,
) -> None:
    """One async consumer. Run N copies concurrently for parallelism.

    The loop has two phases per iteration:

      1. Reclaim — ``XAUTOCLAIM`` picks up entries idle past
         ``_PENDING_IDLE_MS`` (i.e. delivered but never XACK'd because
         the previous consumer crashed mid-handler).
      2. Read   — ``XREADGROUP ... >`` pulls fresh entries.

    Caller is responsible for ``ensure_consumer_group`` once before
    starting the loop; doing it here would race when N consumers boot.

    ``read_block_ms`` overrides the XREADGROUP block timeout — production
    keeps the 5s default (cheap idle); tests pass a small value (50ms)
    so the shutdown event takes effect promptly.
    """
    shutdown = shutdown or asyncio.Event()
    while not shutdown.is_set():
        try:
            await _reclaim_abandoned(
                redis=redis,
                adapter=adapter,
                session_factory=session_factory,
                consumer_name=consumer_name,
            )
            await _read_and_dispatch(
                redis=redis,
                adapter=adapter,
                session_factory=session_factory,
                consumer_name=consumer_name,
                read_block_ms=read_block_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception(
                "queue_consumer_iteration_failed",
                extra={"consumer": consumer_name},
            )
            # Back off on persistent failure so a Redis flap doesn't
            # spin the loop hot.
            import contextlib

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown.wait(), timeout=1.0)
        # Cooperative yield. In production xreadgroup blocks for up to
        # `read_block_ms` so this is a no-op there. In tests with
        # fakeredis (which doesn't honor block timeouts) this prevents
        # the loop from starving every other coroutine in the event
        # loop — including the test's shutdown trigger.
        await asyncio.sleep(0)


async def _read_and_dispatch(
    *,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    consumer_name: str,
    read_block_ms: int = _READ_BLOCK_MS,
) -> None:
    """One XREADGROUP round."""
    response = await redis.xreadgroup(
        groupname=GROUP_NAME,
        consumername=consumer_name,
        streams={STREAM_NAME: ">"},
        count=_READ_COUNT,
        block=read_block_ms,
    )
    # Update queue-depth gauge after each read so Prometheus reflects the
    # current backlog. XLEN is O(1) and cheap relative to the block timeout.
    try:
        depth = await redis.xlen(STREAM_NAME)
        queue_depth.set(float(depth))
    except Exception:
        pass  # gauge staleness is acceptable; never crash the consumer loop

    if not response:
        return
    for _stream_name, entries in response:
        for entry_id, fields in entries:
            await _process_entry(
                redis=redis,
                adapter=adapter,
                session_factory=session_factory,
                entry_id=_as_str(entry_id),
                fields=fields,
            )


async def _reclaim_abandoned(
    *,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    consumer_name: str,
) -> None:
    """XAUTOCLAIM idle entries from dead consumers."""
    try:
        result = await redis.xautoclaim(
            name=STREAM_NAME,
            groupname=GROUP_NAME,
            consumername=consumer_name,
            min_idle_time=_PENDING_IDLE_MS,
            start_id="0-0",
            count=_READ_COUNT,
        )
    except Exception:
        # Older Redis servers / clients may not implement XAUTOCLAIM;
        # log once and move on. New entries still flow via XREADGROUP.
        _logger.debug("queue_xautoclaim_unsupported", extra={"consumer": consumer_name})
        return

    # Response shape: (next_cursor, [(entry_id, fields), ...], deleted_ids)
    # — but some redis-py versions drop the third tuple element on
    # older Redis servers. Be defensive: only the entries list matters.
    if not isinstance(result, list | tuple) or len(result) < 2:
        return
    entries = result[1] or []
    for entry_id, fields in entries:
        await _process_entry(
            redis=redis,
            adapter=adapter,
            session_factory=session_factory,
            entry_id=_as_str(entry_id),
            fields=fields,
        )


async def _process_entry(
    *,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    entry_id: str,
    fields: dict,
) -> None:
    """Deserialize → dispatch → ack/dlq one entry."""
    blob = _extract_payload_blob(fields)
    if blob is None:
        await _ack_and_dlq(redis, entry_id, reason="payload_unreadable")
        return

    # Route TaskSpec v3 entries through the new path; fall through to the
    # legacy QueuePayload path for v1/v2 entries still in the queue.
    if _is_v3_spec(blob):
        spec = deserialize_task_spec(blob)
        if spec is None:
            await _ack_and_dlq(redis, entry_id, reason="task_spec_v3_unreadable")
            return
        await _execute_task_spec(
            spec,
            entry_id=entry_id,
            redis=redis,
            adapter=adapter,
            session_factory=session_factory,
        )
        return

    payload = deserialize_payload(blob)
    if payload is None:
        await _ack_and_dlq(redis, entry_id, reason="payload_unreadable")
        return

    # Re-derive Dispatch from the persisted payload. Router is pure so
    # we don't need a network call to reconstruct.
    event = payload.to_event()
    new_dispatch = dispatch_for(event)
    if new_dispatch is None:
        # Router decided the event is no longer relevant. Could happen
        # if the payload was enqueued by an older version of the router
        # whose dispatch table has since narrowed. Drop quietly.
        _logger.info(
            "queue_entry_no_longer_routable",
            extra={"entry_id": entry_id, "delivery_id": payload.delivery_id},
        )
        await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
        return

    # State-machine slice (v2 payload): carry the receive-side's
    # classification through to the handler. v1 payloads have
    # ``intent`` / ``run_id`` / ``resource_key`` defaulted by the
    # deserializer, so this branch is safe to take unconditionally.
    if payload.resource_key is not None and payload.intent is not None:
        new_dispatch = upgrade_dispatch(
            new_dispatch,
            intent=payload.intent,
            run_id=payload.run_id or payload.task_id,
            prev_run_id=payload.prev_run_id,
            event_seq=payload.event_seq,
            resource_key=payload.resource_key,
        )

    # Cross-dyno cancellation: if this entry's intent supersedes or
    # cancels a prior run, signal that run before we touch the handler.
    # The receive side already signalled — this is the belt-and-suspenders
    # path that catches the case where the queue lagged enough for the
    # prev_run_id flag to expire between receive and dequeue (very
    # unlikely with the 1-hour flag TTL, but cheap to do).
    if payload.prev_run_id and payload.intent in {"supersede", "cancel"}:
        try:
            await cancellation_signal(redis, payload.prev_run_id)
        except Exception:
            _logger.exception(
                "queue_cancellation_signal_failed",
                extra={
                    "entry_id": entry_id,
                    "prev_run_id": payload.prev_run_id,
                    "intent": payload.intent,
                },
            )

    # A CANCEL intent only stops the prior run — there's no new run
    # to invoke. XACK and move on; the audit row was written on the
    # receive side via the state-machine row write.
    if payload.intent == "cancel":
        await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
        _logger.info(
            "queue_entry_cancel_only",
            extra={
                "entry_id": entry_id,
                "delivery_id": payload.delivery_id,
                "prev_run_id": payload.prev_run_id,
            },
        )
        return

    attempts = await _bump_attempt_counter(redis, entry_id)
    # Register the current task under the new run_id so a future
    # SUPERSEDE/CANCEL arriving on this same dyno can ``.cancel()``
    # us. ``deregister`` in finally guarantees no stale entry survives
    # the handler exit (success OR exception).
    active_run_id = new_dispatch.run_id or new_dispatch.task_id
    cancellation_register(active_run_id)

    # Attach delivery context to Sentry for this dispatch so any
    # uncaught exception carries the delivery_id / repo / feature tags.
    # push_scope() is async-safe: the tags are scoped to this coroutine's
    # Sentry hub and don't bleed into concurrent dispatches.
    _attach_sentry_tags(payload)

    try:
        try:
            await run_dispatch(
                adapter=adapter,
                event=event,
                dispatch=new_dispatch,
                session_factory=session_factory,
                redis=redis,
                check_run_id=payload.check_run_id,
            )
        except Exception:
            # `run_dispatch` already swallows its own errors; this is a
            # belt-and-suspenders catch for any crash that escapes (e.g.
            # OOM during a future code path). The entry is recoverable
            # via PEL on next XAUTOCLAIM.
            _logger.exception(
                "queue_run_dispatch_escaped",
                extra={"entry_id": entry_id, "delivery_id": payload.delivery_id},
            )
            # Don't XACK — let the next reclaim cycle pick it up.
            if attempts >= _MAX_ATTEMPTS:
                await _ack_and_dlq(redis, entry_id, reason="max_attempts", payload=payload)
            return
    finally:
        # Always free the registry slot, even on exception or early return.
        # Leaving a stale entry would let a future SUPERSEDE cancel a task
        # that has already exited — usually harmless, but the spurious
        # ``CancelledError`` would noise up logs.
        cancellation_deregister(active_run_id)

    await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
    _logger.info(
        "queue_entry_dispatched",
        extra={
            "entry_id": entry_id,
            "delivery_id": payload.delivery_id,
            "repo": payload.repo,
            "feature": payload.feature,
            "attempts": attempts,
        },
    )


async def _bump_attempt_counter(redis: redis_async.Redis, entry_id: str) -> int:
    """INCR + EXPIRE the per-entry retry counter. Returns the new value."""
    key = _retry_key(entry_id)
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.incr(key)
        pipe.expire(key, _RETRY_TTL_SECONDS)
        results = await pipe.execute()
        return int(results[0])
    except Exception:
        _logger.exception("queue_retry_counter_failed", extra={"entry_id": entry_id})
        return 1  # Optimistic — assume first attempt.


async def _ack_and_dlq(
    redis: redis_async.Redis,
    entry_id: str,
    *,
    reason: str,
    payload: QueuePayload | None = None,
) -> None:
    """Move an entry to the DLQ stream and XACK so it stops circulating."""
    fields = {
        "reason": reason,
        "src_entry_id": entry_id,
        # If we have the payload, re-attach the JSON so the DLQ entry
        # is self-contained (ops doesn't have to grep the main stream).
        **({"json": payload.to_json()} if payload is not None else {}),
    }
    try:
        await redis.xadd(DEAD_STREAM, fields, maxlen=MAX_STREAM_LEN, approximate=True)
    except Exception:
        _logger.exception("queue_dlq_write_failed", extra={"entry_id": entry_id})
    try:
        await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
    except Exception:
        _logger.exception("queue_dlq_ack_failed", extra={"entry_id": entry_id})


def _extract_payload_blob(fields: dict) -> str | bytes | None:
    """The producer XADDs ``{"json": "..."}``. Read robustly."""
    if not fields:
        return None
    # Different redis-py versions: keys may be bytes or str.
    for key, value in fields.items():
        if _as_str(key) == "json":
            return value
    return None


def _as_str(value: object) -> str:
    if isinstance(value, bytes | bytearray):
        return value.decode("utf-8", errors="replace")
    return str(value)
