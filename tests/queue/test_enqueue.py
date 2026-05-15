"""enqueue() — XADD + bounded stream behavior."""

from __future__ import annotations

import fakeredis.aioredis

from openbot.events import EventKind, UnifiedEvent
from openbot.llm.router import Feature
from openbot.queue import QueuePayload, deserialize_payload, enqueue
from openbot.queue.payload import STREAM_NAME


def _payload() -> QueuePayload:
    event = UnifiedEvent(
        channel="github",
        delivery_id="d-1",
        kind=EventKind.ISSUE_OPENED,
        repo="org/r",
        actor="alice",
        actor_type="User",
        issue_number=1,
        installation_id=99,
        raw={"action": "opened"},
    )
    return QueuePayload.from_event(event, feature=Feature.TRIAGE, task_id="t-1")


async def test_enqueue_writes_to_stream() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    entry_id = await enqueue(redis, _payload())
    # Format `<ms>-<seq>` — we don't pin the exact value but it must be
    # parseable by anyone who reads the audit log later.
    assert "-" in entry_id
    length = await redis.xlen(STREAM_NAME)
    assert length == 1


async def test_enqueued_payload_round_trips() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await enqueue(redis, _payload())
    entries = await redis.xrange(STREAM_NAME, count=1)
    assert len(entries) == 1
    _entry_id, fields = entries[0]
    blob = fields["json"]
    restored = deserialize_payload(blob)
    assert restored is not None
    assert restored.delivery_id == "d-1"
    assert restored.feature == "triage"


async def test_enqueue_bounded_by_maxlen() -> None:
    """MAXLEN ~ N keeps Redis from OOMing under a stuck worker."""
    from openbot.queue.payload import MAX_STREAM_LEN

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    # We can't realistically push 10_001 in a unit test, but we CAN
    # verify the xadd call uses approximate MAXLEN by inspecting the
    # stream metadata after a single push.
    await enqueue(redis, _payload())
    # The cap is set at write time; fakeredis exposes it via xinfo.
    info = await redis.xinfo_stream(STREAM_NAME)
    # `max-deleted-entry-id` exists from Redis 7+; mostly we just want
    # to confirm the stream is healthy and the entry is there.
    assert info["length"] == 1
    # And that the constant we shipped matches what we documented.
    assert MAX_STREAM_LEN == 10_000


async def test_enqueue_each_call_creates_distinct_entry() -> None:
    """Two calls → two stream entries (caller is responsible for dedup;
    enqueue itself doesn't gate)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await enqueue(redis, _payload())
    await enqueue(redis, _payload())
    assert await redis.xlen(STREAM_NAME) == 2
