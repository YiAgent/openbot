"""State-machine L2: issue lifecycle (I-01, I-05x2, I-09, I-11, I-23, M-31).

Plan IDs reference ``docs/plans/webhook-worker-test-plan.md``.

Router gaps (events ``dispatch_for`` returns None for — not routed to SM):
  I-04: ISSUE_CLOSED  — no routing; deferred.
  I-05 fresh path: ISSUE_REOPENED — not in dispatch_for's routing table.
    ``test_reopened_from_idle`` verifies the router correctly ignores it.
    To test the SM "fresh reopen" path, the router must be extended first.

  The "IGNORE while already RUNNING" path (I-05 running) IS testable via
  a second ISSUE_OPENED with a different delivery_id — same event kind,
  different delivery.
"""

from __future__ import annotations

from openbot.infrastructure.persistence.models import State
from tests.state_machine._payloads import _REPO, issue_assigned_body, issue_body, sign
from tests.state_machine.conftest import SMHarness

# Resource key for the default issue (number=42, channel=github, repo=_REPO).
_ISSUE_RK = f"github:{_REPO}:issue:42"


# ── I-01: issues.opened → TRIAGE, RUNNING ─────────────────────────────────


async def test_opened_starts_task(sm: SMHarness) -> None:
    """I-01: first issues.opened → DB RUNNING, one entry enqueued, feature=triage."""
    body = issue_body("opened", number=42)
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="issues", delivery="d-01"),
    )

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["feature"] == "triage"
    assert await sm.queue_len() == 1
    assert await sm.db_state(_ISSUE_RK) == State.RUNNING


# ── I-05: issues.reopened → fresh or already_running ──────────────────────


async def test_reopened_from_idle_starts_triage(sm: SMHarness) -> None:
    """I-05 (fresh): ISSUE_REOPENED from idle → triage START (same as ISSUE_OPENED).

    ``dispatch_for`` routes ISSUE_OPENED, ISSUE_EDITED, and ISSUE_REOPENED
    all to TRIAGE.  A reopened issue with no prior run lands in RUNNING state
    via the START intent — identical to a fresh ISSUE_OPENED.
    """
    body = issue_body("reopened", number=42)
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="issues", delivery="d-05-fresh"),
    )

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted", f"expected accepted, got: {data}"
    assert data["feature"] == "triage"
    assert await sm.queue_len() == 1
    assert await sm.db_state(_ISSUE_RK) == State.RUNNING


async def test_opened_twice_second_ignored_already_running(sm: SMHarness) -> None:
    """I-05 (running): second ISSUE_OPENED while RUNNING → SM IGNORE(already_running).

    Two distinct delivery IDs (different commits/retries from GitHub) for
    the same issue both pass dedup but the state machine classifies the
    second as IGNORE because a run is already in flight.
    """
    # First delivery: starts the run.
    body1 = issue_body("opened", number=42)
    await sm.client.post(
        "/webhook/github",
        content=body1,
        headers=sign(body1, event="issues", delivery="d-05-open-1"),
    )
    assert await sm.db_state(_ISSUE_RK) == State.RUNNING

    # Second delivery: same event kind, different delivery_id → bypasses dedup,
    # reaches state machine which returns IGNORE + reason=already_running.
    body2 = issue_body("opened", number=42)
    resp = await sm.client.post(
        "/webhook/github",
        content=body2,
        headers=sign(body2, event="issues", delivery="d-05-open-2"),
    )

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "already_running"
    # Queue must NOT have grown — the state machine returned IGNORE.
    assert await sm.queue_len() == 1


# ── I-09: issues.labeled (not in event table) → ignored ───────────────────


async def test_unrelated_label_ignored(sm: SMHarness) -> None:
    """I-09: issues.labeled maps to UNKNOWN kind → router returns None → ignored."""
    body = issue_body("labeled", number=42)
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="issues", delivery="d-09"),
    )

    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0


# ── I-11: issues.assigned to a human → ignored ────────────────────────────


async def test_human_assigned_ignored(sm: SMHarness) -> None:
    """I-11: issues.assigned with human assignee → router returns None → ignored."""
    body = issue_assigned_body(number=42, bot_assignee=False)
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="issues", delivery="d-11"),
    )

    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0


# ── I-23: duplicate delivery → second ignored ─────────────────────────────


async def test_duplicate_delivery_deduped(sm: SMHarness) -> None:
    """I-23: same X-GitHub-Delivery twice → second returns status=duplicate."""
    body = issue_body("opened", number=42)
    headers = sign(body, event="issues", delivery="d-23-dup")

    # First delivery — accepted and queued.
    resp1 = await sm.client.post("/webhook/github", content=body, headers=headers)
    assert resp1.json()["status"] == "accepted"
    assert await sm.queue_len() == 1

    # Same delivery again — dedup fires.
    resp2 = await sm.client.post("/webhook/github", content=body, headers=headers)
    assert resp2.status_code == 202
    assert resp2.json()["status"] == "duplicate"
    # Queue must NOT have grown.
    assert await sm.queue_len() == 1


# ── M-31: bot actor → router drops before state machine ───────────────────


async def test_bot_actor_ignored(sm: SMHarness) -> None:
    """M-31: issues.opened by a Bot sender → is_from_bot=True → router returns None."""
    body = issue_body("opened", number=42, actor_type="Bot")
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="issues", delivery="d-m31"),
    )

    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0
    # No DB row created — the event never reached the state machine.
    assert await sm.db_state(_ISSUE_RK) == State.IDLE
