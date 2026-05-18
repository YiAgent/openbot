"""Integration L3: I-24 — Redis write-ordering guarantee.

The webapp's write sequence for every accepted webhook is:

  1. ``resource_lock`` acquired (Redis SET NX).
  2. ``transition()`` classifies + writes ``task_runs`` row.
  3. ``session.commit()`` — DB committed (still under the lock).
  4. ``enqueue()`` — XADD to the Redis stream.
  5. ``resource_lock`` released.

The invariant we test: **step 3 always precedes step 4**. A worker that
dequeues entry N from the stream must already find the matching
``task_runs`` row in the DB with ``current_run_id == payload.run_id``.

Why this matters: if XADD happened before the DB commit, a worker could
dequeue the entry and then find no DB row (or a stale row from an earlier
run), causing incorrect state-machine behaviour — for example treating a
SUPERSEDE as a fresh START.

Test strategy: after each accepted webhook POST we:
  1. Read the stream entry that was just appended.
  2. Deserialize the ``QueuePayload`` to extract ``resource_key`` and
     ``run_id``.
  3. Query the DB and assert ``current_run_id == payload.run_id``.

No worker is started — these tests validate the ordering purely from the
receive side.
"""

from __future__ import annotations

from openbot.queue import STREAM_NAME, deserialize_payload
from tests.state_machine._payloads import _REPO, issue_body, pr_body, sign

from .conftest import SMHarness

_PR_RK = f"github:{_REPO}:pr:7"
_ISSUE_RK = f"github:{_REPO}:issue:42"


# ── I-24a: PR opened — DB committed before XADD ───────────────────────────


async def test_pr_opened_db_committed_before_stream_entry(sm: SMHarness) -> None:
    """I-24: pull_request.opened → DB row exists with correct run_id
    BEFORE any worker reads the stream entry."""
    body = pr_body("opened", number=7, head_sha="sha-i24a")
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="pull_request", delivery="i24-pr-open"),
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"

    # There must be exactly one stream entry.
    entries = await sm.redis.xrange(STREAM_NAME, count=5)
    assert len(entries) == 1, "Expected exactly one stream entry after PR open"
    _entry_id, fields = entries[0]

    # Deserialize the queue payload from the entry.
    payload = deserialize_payload(fields["json"])
    assert payload is not None, "Stream entry must contain a valid JSON payload"
    assert payload.resource_key == _PR_RK
    assert payload.run_id is not None, "v2 payload must carry a non-null run_id"

    # The DB row MUST already exist with run_id == payload.run_id.
    # If this fails, the XADD happened before the DB commit.
    db_run_id = await sm.db_run_id(_PR_RK)
    assert db_run_id == payload.run_id, (
        f"Ordering violation: DB has run_id={db_run_id!r} but "
        f"stream payload carries run_id={payload.run_id!r}. "
        "The stream XADD must happen AFTER session.commit()."
    )

    # The payload's intent must be 'start' (fresh PR).
    assert payload.intent == "start"
    # No prior run to supersede.
    assert payload.prev_run_id is None


# ── I-24b: Issue opened — same guarantee for the issues path ──────────────


async def test_issue_opened_db_committed_before_stream_entry(sm: SMHarness) -> None:
    """I-24 (issue variant): issues.opened → DB run_id == payload.run_id
    before any worker sees the entry."""
    body = issue_body("opened", number=42)
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="issues", delivery="i24-issue-open"),
    )
    assert resp.status_code == 202

    entries = await sm.redis.xrange(STREAM_NAME, count=5)
    assert len(entries) == 1
    _entry_id, fields = entries[0]

    payload = deserialize_payload(fields["json"])
    assert payload is not None
    assert payload.resource_key == _ISSUE_RK
    assert payload.run_id is not None

    db_run_id = await sm.db_run_id(_ISSUE_RK)
    assert (
        db_run_id == payload.run_id
    ), "Ordering violation: issue stream entry predates the DB commit"


# ── I-24c: SUPERSEDE — new entry's run_id matches post-commit DB state ────


async def test_supersede_stream_payload_matches_db_run_id(sm: SMHarness) -> None:
    """I-24 (supersede): after SUPERSEDE, the stream carries the NEW
    run_id and the DB is already updated to match before any worker sees it.

    Also verifies ``prev_run_id`` in the payload links back to the run that
    was superseded — the worker needs this to signal cancellation.
    """
    # Step 1: open the PR → START.
    open_body = pr_body("opened", number=7, head_sha="sha-open-i24c")
    await sm.client.post(
        "/webhook/github",
        content=open_body,
        headers=sign(open_body, event="pull_request", delivery="i24c-open"),
    )
    run_id_open = await sm.db_run_id(_PR_RK)
    assert run_id_open is not None

    # Step 2: push new commit → SUPERSEDE.
    sync_body = pr_body("synchronize", number=7, head_sha="sha-sync-i24c")
    await sm.client.post(
        "/webhook/github",
        content=sync_body,
        headers=sign(sync_body, event="pull_request", delivery="i24c-sync"),
    )

    # Read both stream entries.
    entries = await sm.redis.xrange(STREAM_NAME, count=10)
    assert len(entries) == 2, "Expected two stream entries: open + sync"

    # The second entry is the superseding one.
    _eid_sync, fields_sync = entries[1]
    payload_sync = deserialize_payload(fields_sync["json"])
    assert payload_sync is not None
    assert payload_sync.intent == "supersede"

    # The superseding payload must link back to the original run.
    assert payload_sync.prev_run_id == run_id_open, (
        f"Superseding entry must carry prev_run_id={run_id_open!r} "
        f"but got {payload_sync.prev_run_id!r}"
    )

    # The DB must already hold the NEW run_id (not the original).
    db_run_id_after = await sm.db_run_id(_PR_RK)
    assert (
        db_run_id_after == payload_sync.run_id
    ), "After SUPERSEDE: DB run_id must equal the superseding payload's run_id"
    # And the new run_id must be different from the original.
    assert db_run_id_after != run_id_open
