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
import logging
from typing import TYPE_CHECKING, Any, Final

from openbot.application.dispatcher import execute_handler
from openbot.application.router import dispatch_for, upgrade_dispatch

# TODO(phase-2c): route through CancellationPort once worker composition root lands.
from openbot.application.state.cancellation import (
    deregister as cancellation_deregister,
)
from openbot.application.state.cancellation import (
    register as cancellation_register,
)
from openbot.application.state.runs_repo import store_reviewed_sha
from openbot.core.metrics import queue_depth
from openbot.dispatcher.classifier import classify_for_dispatch, parse_classifier_output
from openbot.infrastructure.config_loader import load_for_repo
from openbot.infrastructure.persistence.db import session_scope
from openbot.infrastructure.queue.payload import (
    DEAD_STREAM,
    GROUP_NAME,
    MAX_STREAM_LEN,
    STREAM_NAME,
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

# Hard wall-clock budget for a single handler execution.  PRD §4.3 allows
# up to 45 min for the fix loop; add a 5 min grace margin.  When the
# timeout fires, asyncio.wait_for cancels execute_handler which then
# PATCHes the check run to ``conclusion=cancelled`` before re-raising
# CancelledError — so the GitHub UI never shows a permanently-stuck check.
_HANDLER_TIMEOUT_SECONDS: Final = 50 * 60  # 50 minutes


def _retry_key(entry_id: str) -> str:
    return f"openbot:workflows:retries:{entry_id}"


async def _execute_task_spec(
    spec: TaskSpec,
    *,
    entry_id: str,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    agent_checkpointer: Any | None = None,
    sandbox_factory: Any | None = None,
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

    # W4.5: Resolve typed classifier output for the handler's policy gate.
    #
    # Two paths:
    #   a) spec.classifier_output is not None — the webhook already ran the
    #      classifier and stored the result as a dict on the TaskSpec. Rehydrate
    #      it into the typed union (TriageClassifierOutput | ReviewClassifierOutput
    #      | …) that ``derive_sandbox_policy`` expects.
    #   b) spec.classifier_output is None (``classifier_skipped`` is True) — the
    #      webhook deliberately deferred classification to avoid blocking the 202
    #      response within GitHub's 10 s deadline.  Run ``classify_for_dispatch``
    #      here; the worker has no deadline.
    #
    # Both paths are fail-open: any exception yields ``None``, which
    # ``derive_sandbox_policy`` treats as "respect the static SandboxPolicy".
    from dataclasses import asdict

    classifier_output = None
    if spec.classifier_output is not None:
        # Path (a): rehydrate the pre-computed dict.
        try:
            classifier_output = parse_classifier_output(
                new_dispatch.feature, spec.classifier_output
            )
        except Exception:
            _logger.warning(
                "queue_v3_classifier_output_rehydrate_failed",
                extra={"entry_id": entry_id, "delivery_id": spec.delivery_id},
                exc_info=True,
            )
    else:
        # Path (b): run the classifier now that we're off the webhook fast path.
        try:
            raw = await classify_for_dispatch(
                event=event, feature=new_dispatch.feature, redis=redis
            )
            if raw is not None:
                classifier_output = parse_classifier_output(new_dispatch.feature, asdict(raw))
        except Exception:
            _logger.warning(
                "queue_v3_classifier_dispatch_failed",
                extra={"entry_id": entry_id, "delivery_id": spec.delivery_id},
                exc_info=True,
            )

    # W5-W8: Attempt counter + cancellation lifecycle.
    attempts = await _bump_attempt_counter(redis, entry_id)
    active_run_id = new_dispatch.run_id or new_dispatch.task_id
    cancellation_register(active_run_id)

    try:
        try:
            # Hard budget: asyncio.wait_for cancels execute_handler when the
            # wall-clock limit is reached.  execute_handler's BaseException
            # guard catches the resulting CancelledError and PATCHes the check
            # run to ``conclusion=cancelled`` before re-raising — so the
            # GitHub UI never shows a permanently-stuck check.  wait_for then
            # converts the inner CancelledError to TimeoutError for us.
            await asyncio.wait_for(
                execute_handler(
                    adapter=adapter,
                    event=event,
                    dispatch=new_dispatch,
                    config=config,
                    session_factory=session_factory,
                    redis=redis,
                    check_run_id=spec.check_run_id,
                    classifier_output=classifier_output,
                    agent_checkpointer=agent_checkpointer,
                    sandbox_factory=sandbox_factory,
                ),
                timeout=_HANDLER_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            # SUPERSEDE / explicit cancel / shutdown. ``execute_handler``
            # already finalized the GitHub check run as ``cancelled`` and
            # re-raised so this branch can XACK — letting the entry sit in
            # the PEL would have reclaim re-run a deliberately-cancelled
            # task ~60 s later. Cancellation is not a failure, so the DLQ
            # is intentionally left alone.
            _logger.info(
                "queue_v3_entry_cancelled",
                extra={"entry_id": entry_id, "delivery_id": spec.delivery_id},
            )
            await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
            return
        except TimeoutError:
            # Budget exceeded — execute_handler already patched the check run
            # as ``cancelled``.  XACK so the entry doesn't stay in the PEL
            # and get reclaimed (a timeout is not a transient failure worth
            # retrying with the same budget guard that just fired).
            _logger.warning(
                "queue_v3_handler_timed_out",
                extra={
                    "entry_id": entry_id,
                    "delivery_id": spec.delivery_id,
                    "timeout_seconds": _HANDLER_TIMEOUT_SECONDS,
                },
            )
            await redis.xack(STREAM_NAME, GROUP_NAME, entry_id)
            return
        except Exception:
            _logger.exception(
                "queue_v3_execute_handler_escaped",
                extra={"entry_id": entry_id, "delivery_id": spec.delivery_id},
            )
            if attempts >= _MAX_ATTEMPTS:
                # Close the check run before DLQ-ing so the GitHub UI doesn't
                # show a permanently-stuck "in_progress" check after all
                # retries are exhausted.
                if spec.check_run_id is not None:
                    try:
                        await adapter.update_check_run(
                            spec.to_event(),
                            spec.check_run_id,
                            status="completed",
                            conclusion="failure",
                            output={
                                "title": "Analysis Failed",
                                "summary": (
                                    f"OpenBot could not complete the `{spec.scenario}` "
                                    "workflow after multiple attempts. The task has been "
                                    "moved to the dead-letter queue."
                                ),
                            },
                        )
                    except Exception:
                        _logger.exception(
                            "check_run_dlq_update_failed",
                            extra={
                                "entry_id": entry_id,
                                "check_run_id": spec.check_run_id,
                            },
                        )
                await _ack_and_dlq(redis, entry_id, reason="max_attempts_v3")
            # Don't XACK — let the next reclaim cycle pick it up.
            return
    finally:
        cancellation_deregister(active_run_id)

    # For completed PR review runs, persist the head SHA so future
    # PR_SYNCHRONIZED events can compute DiffScope.is_incremental correctly.
    if spec.scenario == "review" and session_factory is not None and spec.resource_key is not None:
        head_sha = ((spec.raw.get("pull_request") or {}).get("head") or {}).get("sha")
        if head_sha:
            try:
                async with session_scope(session_factory) as _session:
                    await store_reviewed_sha(_session, spec.resource_key, str(head_sha))
            except Exception:
                _logger.exception(
                    "queue_v3_store_reviewed_sha_failed",
                    extra={"entry_id": entry_id, "delivery_id": spec.delivery_id},
                )

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
    agent_checkpointer: Any | None = None,
    sandbox_factory: Any | None = None,
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
                agent_checkpointer=agent_checkpointer,
                sandbox_factory=sandbox_factory,
            )
            await _read_and_dispatch(
                redis=redis,
                adapter=adapter,
                session_factory=session_factory,
                consumer_name=consumer_name,
                read_block_ms=read_block_ms,
                agent_checkpointer=agent_checkpointer,
                sandbox_factory=sandbox_factory,
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
    agent_checkpointer: Any | None = None,
    sandbox_factory: Any | None = None,
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
                agent_checkpointer=agent_checkpointer,
                sandbox_factory=sandbox_factory,
            )


async def _reclaim_abandoned(
    *,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    consumer_name: str,
    agent_checkpointer: Any | None = None,
    sandbox_factory: Any | None = None,
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
            agent_checkpointer=agent_checkpointer,
            sandbox_factory=sandbox_factory,
        )


async def _process_entry(
    *,
    redis: redis_async.Redis,
    adapter: GitHubAdapter,
    session_factory: async_sessionmaker[AsyncSession] | None,
    entry_id: str,
    fields: dict,
    agent_checkpointer: Any | None = None,
    sandbox_factory: Any | None = None,
) -> None:
    """Deserialize TaskSpec v3 → dispatch → ack/dlq one entry."""
    blob = _extract_payload_blob(fields)
    if blob is None:
        await _ack_and_dlq(redis, entry_id, reason="payload_unreadable")
        return

    spec = deserialize_task_spec(blob)
    if spec is None:
        await _ack_and_dlq(redis, entry_id, reason="task_spec_unreadable")
        return

    await _execute_task_spec(
        spec,
        entry_id=entry_id,
        redis=redis,
        adapter=adapter,
        session_factory=session_factory,
        agent_checkpointer=agent_checkpointer,
        sandbox_factory=sandbox_factory,
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
) -> None:
    """Move an entry to the DLQ stream and XACK so it stops circulating."""
    fields = {
        "reason": reason,
        "src_entry_id": entry_id,
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
