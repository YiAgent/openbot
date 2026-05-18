"""Integration L3: I-20, P-51 — concurrent supersede correctness.

Two or three webhook deliveries for the same resource fire concurrently
via ``asyncio.gather``. The ``resource_lock`` (Redis SET NX) and the
SQLAlchemy ``version_id_col`` CAS on ``task_runs`` are the two layers
that prevent race conditions.

Expected behaviour under concurrent load:
  - The resource_lock serialises classify → CAS-write → enqueue so no
    two events see each other's partial state.
  - Exactly one event ends up as the winner (RUNNING); the rest are
    either IGNORE (for same-kind repeats like ISSUE_OPENED) or
    SUPERSEDE (for intent-escalating events like PR_SYNCHRONIZED).
  - Cancel flags are set for all superseded run_ids.

Test matrix
-----------
  P-51  Two concurrent PR_SYNCHRONIZED → one wins, one is superseded.
  I-20  Three concurrent PR_SYNCHRONIZED → one wins, two superseded.
  I-20* Two concurrent ISSUE_OPENED → one wins (START), one ignored
        (already_running) — ISSUE_OPENED is an idempotent-start event.
"""

from __future__ import annotations

import asyncio

from openbot.infrastructure.persistence.models import State
from openbot.infrastructure.queue import STREAM_NAME, deserialize_payload
from tests.state_machine._payloads import _REPO, issue_body, pr_body, sign

from .conftest import SMHarness

_PR_RK = f"github:{_REPO}:pr:7"
_ISSUE_RK = f"github:{_REPO}:issue:42"


async def _stream_run_ids(sm: SMHarness, *, skip: int = 0) -> list[str]:
    """Read run_ids from all current stream entries (skipping the first ``skip``).

    The stream stores ``QueuePayload`` JSON. We deserialize it to get the
    ``run_id`` field (the state-machine run identifier) rather than relying
    on the HTTP response's ``task_id`` (which is the delivery-derived id —
    a different identifier space).
    """
    entries = await sm.redis.xrange(STREAM_NAME, count=20)
    run_ids = []
    for _, fields in entries[skip:]:
        p = deserialize_payload(fields["json"])
        if p and p.run_id:
            run_ids.append(p.run_id)
    return run_ids


async def _sync(sm: SMHarness, *, sha: str, delivery: str) -> dict:
    """POST one pull_request.synchronize and return parsed response dict."""
    body = pr_body("synchronize", number=7, head_sha=sha)
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="pull_request", delivery=delivery),
    )
    return {"code": resp.status_code, **resp.json()}


# ── P-51: two concurrent synchronize events ───────────────────────────────


async def test_two_concurrent_synchronize_one_wins(sm: SMHarness) -> None:
    """P-51: two PR synchronize events fired concurrently — exactly one
    ends up RUNNING; the seeded open run is superseded; cancel flags set."""
    # Seed: open the PR (serial) so there is an existing RUNNING run.
    open_body = pr_body("opened", number=7, head_sha="sha-base-p51")
    await sm.client.post(
        "/webhook/github",
        content=open_body,
        headers=sign(open_body, event="pull_request", delivery="p51-open"),
    )
    run_id_open = await sm.db_run_id(_PR_RK)
    assert run_id_open is not None

    # Fire two synchronize events concurrently.
    r_a, r_b = await asyncio.gather(
        _sync(sm, sha="sha-A", delivery="p51-sync-A"),
        _sync(sm, sha="sha-B", delivery="p51-sync-B"),
    )

    # Both must be 202 accepted — neither may 4xx/5xx.
    assert r_a["code"] == 202, f"sync-A returned HTTP {r_a['code']}"
    assert r_b["code"] == 202, f"sync-B returned HTTP {r_b['code']}"
    assert r_a["status"] == "accepted", f"sync-A status: {r_a['status']}"
    assert r_b["status"] == "accepted", f"sync-B status: {r_b['status']}"

    # Queue: open + sync-A + sync-B = 3 entries.
    assert await sm.queue_len() == 3

    # DB: still RUNNING with exactly one final run_id.
    assert await sm.db_state(_PR_RK) == State.RUNNING
    final_run_id = await sm.db_run_id(_PR_RK)
    assert final_run_id is not None

    # The opening run MUST be superseded (cancel flag set).
    assert await sm.cancel_flag(run_id_open) is True

    # The final winner must NOT be cancelled.
    assert await sm.cancel_flag(final_run_id) is False

    # Read the run_ids from the two synchronize stream entries (skip entry 0 = open).
    # Note: the HTTP response's ``task_id`` is the delivery-derived identifier —
    # NOT the state-machine ``run_id`` stored in the DB and stream payload.
    # We must read from the stream to get the actual run_ids.
    sync_run_ids = await _stream_run_ids(sm, skip=1)  # skip the open entry
    assert len(sync_run_ids) == 2, f"Expected 2 sync run_ids, got {sync_run_ids}"
    assert final_run_id in sync_run_ids, (
        f"Final DB run_id={final_run_id!r} must be one of the sync run_ids={sync_run_ids}"
    )

    # The losing sync run MUST have a cancel flag.
    losing_id = (set(sync_run_ids) - {final_run_id}).pop()
    assert await sm.cancel_flag(losing_id) is True, (
        f"Expected cancel flag for superseded sync run_id={losing_id!r}"
    )


# ── I-20 (adapted): three concurrent synchronize events ───────────────────


async def test_three_concurrent_synchronize_one_wins(sm: SMHarness) -> None:
    """I-20 (adapted): three concurrent PR_SYNCHRONIZED events — the
    resource_lock serialises all three; exactly one ends up RUNNING and
    the other two have cancel flags set."""
    # Seed.
    open_body = pr_body("opened", number=7, head_sha="sha-base-i20")
    await sm.client.post(
        "/webhook/github",
        content=open_body,
        headers=sign(open_body, event="pull_request", delivery="i20-open"),
    )
    run_id_open = await sm.db_run_id(_PR_RK)

    # Fire three synchronize events concurrently.
    r_x, r_y, r_z = await asyncio.gather(
        _sync(sm, sha="sha-X", delivery="i20-sync-X"),
        _sync(sm, sha="sha-Y", delivery="i20-sync-Y"),
        _sync(sm, sha="sha-Z", delivery="i20-sync-Z"),
    )
    results = [r_x, r_y, r_z]

    # All three must be 202 accepted.
    for r in results:
        assert r["code"] == 202
        assert r["status"] == "accepted"

    # Queue: open + 3 syncs = 4.
    assert await sm.queue_len() == 4

    # DB: RUNNING, one final run_id.
    assert await sm.db_state(_PR_RK) == State.RUNNING
    final_run_id = await sm.db_run_id(_PR_RK)
    assert final_run_id is not None

    # Opening run superseded.
    assert await sm.cancel_flag(run_id_open) is True

    # Read actual run_ids from the stream payloads (skip entry 0 = open).
    sync_run_ids = await _stream_run_ids(sm, skip=1)
    assert len(sync_run_ids) == 3, f"Expected 3 sync run_ids, got {sync_run_ids}"
    assert final_run_id in sync_run_ids

    # The two non-winning sync run_ids must be cancelled.
    for rid in set(sync_run_ids) - {final_run_id}:
        assert await sm.cancel_flag(rid) is True, (
            f"Expected cancel flag for losing sync run_id={rid!r}"
        )

    # Winner has no cancel flag.
    assert await sm.cancel_flag(final_run_id) is False


# ── I-20* issue variant: two concurrent ISSUE_OPENED ─────────────────────


async def test_concurrent_issue_opened_no_double_start(sm: SMHarness) -> None:
    """I-20 (issue variant): two concurrent ISSUE_OPENED for the same
    issue — the resource_lock ensures exactly one wins as START; the second
    sees RUNNING state and returns IGNORE (already_running), not a second
    START. No double-queueing, no double-RUNNING.

    Note: ISSUE_OPENED is an idempotent-start event. Unlike
    PR_SYNCHRONIZED (which always supersedes), a second ISSUE_OPENED
    while RUNNING is classified as IGNORE by the state machine.
    """
    body_a = issue_body("opened", number=42)
    body_b = issue_body("opened", number=42)

    resp_a, resp_b = await asyncio.gather(
        sm.client.post(
            "/webhook/github",
            content=body_a,
            headers=sign(body_a, event="issues", delivery="conc-issue-A"),
        ),
        sm.client.post(
            "/webhook/github",
            content=body_b,
            headers=sign(body_b, event="issues", delivery="conc-issue-B"),
        ),
    )

    # Both must return 202.
    assert resp_a.status_code == 202
    assert resp_b.status_code == 202

    data_a = resp_a.json()
    data_b = resp_b.json()

    # Exactly one accepted (START) + one ignored (already_running).
    statuses = sorted([data_a["status"], data_b["status"]])
    assert statuses == ["accepted", "ignored"], (
        f"Expected exactly one 'accepted' and one 'ignored', got {statuses}. "
        "The resource_lock + state machine must prevent double-START."
    )

    # The ignored one must carry reason=already_running.
    ignored = data_a if data_a["status"] == "ignored" else data_b
    assert ignored.get("reason") == "already_running", (
        f"Expected reason='already_running', got {ignored.get('reason')!r}"
    )

    # DB must have exactly one RUNNING state.
    assert await sm.db_state(_ISSUE_RK) == State.RUNNING

    # Queue must have exactly one entry (the ignored event is not enqueued).
    assert await sm.queue_len() == 1, (
        "Only the accepted (START) event must be enqueued; "
        "the ignored event must not produce a queue entry."
    )
