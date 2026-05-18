"""Integration L3: I-33 — worker crash recovery via XAUTOCLAIM.

Redis Streams give each delivery a PEL (Pending Entry List) entry: the
entry stays in the PEL until the consumer calls XACK. If the consumer
crashes before XACK'ing, the entry sits in the PEL flagged as "idle".

The worker reclaims idle PEL entries via ``XAUTOCLAIM``:
  1. Consumer A reads the entry (XREADGROUP → PEL entry created).
  2. Consumer A's handler raises → not XACK'd → entry remains in PEL.
  3. Consumer B boots; ``_reclaim_abandoned`` calls XAUTOCLAIM with
     ``min_idle_time=_PENDING_IDLE_MS`` → PEL entry transferred to B.
  4. Consumer B retries the handler → success → XACK → PEL cleared.

Test environment:
  - ``_PENDING_IDLE_MS`` is monkeypatched to 0 so any PEL entry is
    immediately claimable — we don't want to wait 60 s in CI.
  - ``run_dispatch`` is patched to a failing stub for consumer A and
    a succeeding stub for consumer B.
  - The stream entry is produced by a real webhook POST so the payload
    roundtrip (serialize → XADD → XREADGROUP → deserialize) is exercised.

DLQ test:
  After ``_MAX_ATTEMPTS`` consecutive failures the entry is moved to
  ``openbot:workflows:dead`` and XACK'd out of the main PEL — no entry
  ever loops forever.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from openbot.queue import STREAM_NAME, consume_loop, ensure_consumer_group
from openbot.queue.payload import DEAD_STREAM, GROUP_NAME
from openbot.queue.worker import _MAX_ATTEMPTS
from tests.state_machine._payloads import pr_body, sign

from .conftest import SMHarness


async def _one_iteration(
    sm: SMHarness,
    *,
    consumer_name: str,
    dispatch_raises: Exception | None = None,
    read_block_ms: int = 50,
) -> None:
    """Run one ``consume_loop`` iteration and wait for it to finish.

    Patches ``openbot.queue.worker.run_dispatch`` for the duration:
      - If ``dispatch_raises`` is given, the mock raises that exception
        (simulates a crashed handler).
      - Otherwise the mock returns None (simulates a successful handler).

    ``read_block_ms=50`` keeps XREADGROUP fast — the production default
    (5 s) would make each test take 5+ seconds.
    """
    mock_dispatch = AsyncMock(
        side_effect=dispatch_raises,
        return_value=None,
    )
    shutdown = asyncio.Event()
    adapter = AsyncMock()

    with patch("openbot.queue.worker.run_dispatch", new=mock_dispatch):
        task = asyncio.create_task(
            consume_loop(
                redis=sm.redis,
                adapter=adapter,
                session_factory=sm.session_factory,
                consumer_name=consumer_name,
                shutdown=shutdown,
                read_block_ms=read_block_ms,
            )
        )
        # Give the loop time to drain at least one read + dispatch round.
        await asyncio.sleep(0.25)
        shutdown.set()
        await asyncio.wait_for(task, timeout=3.0)


# ── I-33a: crash + recovery ───────────────────────────────────────────────


async def test_crashed_consumer_entry_reclaimed_by_recovery_consumer(
    sm: SMHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-33: an entry not XACK'd by a crashed consumer A is reclaimed
    by consumer B via XAUTOCLAIM and successfully processed.

    Sequence:
      1. Consumer group created (MKSTREAM).
      2. Webhook POST → entry XADD'd to stream.
      3. Consumer A reads the entry, handler raises → entry NOT XACK'd.
      4. PEL still has the entry (consumer A owns it, idle >= 0 ms).
      5. Consumer B runs _reclaim_abandoned → XAUTOCLAIM transfers entry.
      6. Consumer B handler succeeds → XACK → PEL empty.
    """
    # Monkeypatch idle threshold to 0 so any PEL entry is immediately
    # claimable — avoids waiting 60 s in tests.
    monkeypatch.setattr("openbot.queue.worker._PENDING_IDLE_MS", 0)

    # Consumer group must exist before the XADD (id="$" means the group
    # starts reading from the stream tip; entries before group creation
    # are not delivered to XREADGROUP).
    await ensure_consumer_group(sm.redis)

    # Post a real webhook to populate the stream with a valid payload.
    body = pr_body("opened", number=7, head_sha="sha-recovery-a")
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="pull_request", delivery="i33-crash"),
    )
    assert resp.status_code == 202
    assert await sm.queue_len() == 1

    # Consumer A "crashes": deliver the entry to its PEL via XREADGROUP
    # WITHOUT processing it. This is equivalent to: consumer received the
    # message → process dies → entry stuck in PEL, never XACK'd.
    delivered = await sm.redis.xreadgroup(
        groupname=GROUP_NAME,
        consumername="consumer-crashed",
        streams={STREAM_NAME: ">"},
        count=1,
        block=200,
    )
    assert delivered, "XREADGROUP must deliver the stream entry to consumer-crashed"

    # PEL must have the entry (owned by consumer-crashed, not XACK'd).
    pending_after_crash = await sm.redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending_after_crash["pending"] >= 1, (
        "Entry must remain in PEL after delivery without XACK — "
        "a crashed consumer must NOT silently discard the work item."
    )

    # Consumer B: XAUTOCLAIM picks up the idle entry; handler succeeds.
    await _one_iteration(
        sm,
        consumer_name="consumer-recovery",
        dispatch_raises=None,  # success
    )

    # PEL must be empty after successful recovery.
    pending_after_recovery = await sm.redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending_after_recovery["pending"] == 0, (
        "Recovery consumer must XACK the reclaimed entry — "
        "PEL must be empty after a successful retry."
    )


# ── I-33b: attempt counter escalates to DLQ ───────────────────────────────


async def test_max_attempts_exhausted_sends_to_dlq(
    sm: SMHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-33 (DLQ path): after ``_MAX_ATTEMPTS`` consecutive handler
    failures the entry is moved to the dead-letter stream and the PEL
    is cleared — no entry loops forever.

    Each failed iteration:
      - Bumps the per-entry retry counter in Redis.
      - If counter >= _MAX_ATTEMPTS: XACK + XADD to DEAD_STREAM.

    The test runs exactly ``_MAX_ATTEMPTS`` failing iterations, then
    asserts the entry is in the DLQ and the main PEL is empty.
    """
    monkeypatch.setattr("openbot.queue.worker._PENDING_IDLE_MS", 0)

    await ensure_consumer_group(sm.redis)

    body = pr_body("opened", number=7, head_sha="sha-dlq-test")
    await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="pull_request", delivery="i33-dlq"),
    )
    assert await sm.queue_len() == 1

    # Deliver entry to consumer-initial's PEL (simulates first delivery without ack).
    # This is the "entry received once" state before the crash loop.
    await sm.redis.xreadgroup(
        groupname=GROUP_NAME,
        consumername="consumer-initial",
        streams={STREAM_NAME: ">"},
        count=1,
        block=200,
    )

    # Run one failing iteration per attempt, each with a distinct consumer name
    # so XAUTOCLAIM transfers the entry to a new consumer each round.
    # With _PENDING_IDLE_MS=0, the entry is claimable immediately.
    for attempt_num in range(_MAX_ATTEMPTS):
        await _one_iteration(
            sm,
            consumer_name=f"consumer-fail-{attempt_num}",
            dispatch_raises=RuntimeError(f"persistent failure #{attempt_num}"),
        )

    # After MAX_ATTEMPTS: entry must be in the DLQ.
    dlq_entries = await sm.redis.xrange(DEAD_STREAM, count=10)
    assert len(dlq_entries) >= 1, (
        f"After {_MAX_ATTEMPTS} failures, the entry must be in DEAD_STREAM. "
        "No DLQ entry found — retry cap may not be enforced."
    )

    # The DLQ entry must carry the failure reason and the source entry_id.
    _, dlq_fields = dlq_entries[-1]
    assert (
        dlq_fields.get("reason") == "max_attempts"
    ), f"DLQ entry reason must be 'max_attempts', got {dlq_fields.get('reason')!r}"

    # PEL must be empty — no entry stuck in the PEL after DLQ.
    pending_final = await sm.redis.xpending(STREAM_NAME, GROUP_NAME)
    assert pending_final["pending"] == 0, (
        "After DLQ, the main stream PEL must be empty — "
        "a DLQ'd entry must be XACK'd out of the PEL."
    )


# ── I-33c: consumer group idempotency on restart ──────────────────────────


async def test_consumer_group_idempotent_on_worker_restart(sm: SMHarness) -> None:
    """I-33 (startup): ``ensure_consumer_group`` is idempotent — a
    worker that restarts and calls it again must not raise on the
    BUSYGROUP response and must not lose existing group state."""
    await ensure_consumer_group(sm.redis)
    await ensure_consumer_group(sm.redis)  # second call — BUSYGROUP, swallowed

    # The group must exist and be queryable.
    groups = await sm.redis.xinfo_groups(STREAM_NAME)
    matching = [g for g in groups if g["name"] == GROUP_NAME]
    assert len(matching) == 1, (
        f"Expected exactly one consumer group named {GROUP_NAME!r}, "
        f"got: {[g['name'] for g in groups]}"
    )
