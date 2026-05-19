"""State-machine L2: PR lifecycle (P-01, P-02x2, P-05).

Plan IDs reference ``docs/plans/webhook-worker-test-plan.md``.

NOTE (P-04): PR_CLOSED / PR_MERGED are not routed by ``dispatch_for()`` —
the webapp returns ``status=ignored`` before the state machine sees them.
Testing P-04 (cancel on close) requires extending the router. Deferred.
"""

from __future__ import annotations

from openbot.infrastructure.persistence.models import State
from tests.state_machine._payloads import _REPO, pr_body, sign
from tests.state_machine.conftest import SMHarness

# Resource key for the default PR (number=7, channel=github, repo=_REPO).
_PR_RK = f"github:{_REPO}:pr:7"


# ── P-01: pull_request.opened → REVIEW, RUNNING ───────────────────────────


async def test_pr_opened_starts_review(sm: SMHarness) -> None:
    """P-01: pull_request.opened → feature=review, DB RUNNING, one entry enqueued."""
    body = pr_body("opened", number=7, head_sha="deadbeef")
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="pull_request", delivery="p-01"),
    )

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["feature"] == "review"
    assert await sm.queue_len() == 1
    assert await sm.db_state(_PR_RK) == State.RUNNING


# ── P-02: pull_request.synchronize ────────────────────────────────────────


async def test_synchronize_from_idle_starts(sm: SMHarness) -> None:
    """P-02 (fresh): synchronize on a brand-new PR → RUNNING, one entry enqueued."""
    body = pr_body("synchronize", number=7, head_sha="sha-new")
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="pull_request", delivery="p-02-fresh"),
    )

    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    assert await sm.queue_len() == 1
    assert await sm.db_state(_PR_RK) == State.RUNNING


async def test_synchronize_supersedes_running(sm: SMHarness) -> None:
    """P-02 (running): synchronize while RUNNING → SUPERSEDE + cancel flag for prev run.

    Sequence:
      1. ``pull_request.opened`` → DB RUNNING, run_id_1 allocated.
      2. ``pull_request.synchronize`` → DB RUNNING (new run_id), queue=2,
         cancel flag set for run_id_1.
    """
    # First delivery: open the PR.
    body1 = pr_body("opened", number=7, head_sha="sha-first")
    await sm.client.post(
        "/webhook/github",
        content=body1,
        headers=sign(body1, event="pull_request", delivery="p-02-open"),
    )
    assert await sm.db_state(_PR_RK) == State.RUNNING
    run_id_1 = await sm.db_run_id(_PR_RK)
    assert run_id_1 is not None

    # Second delivery: new commit pushed → synchronize.
    body2 = pr_body("synchronize", number=7, head_sha="sha-second")
    resp = await sm.client.post(
        "/webhook/github",
        content=body2,
        headers=sign(body2, event="pull_request", delivery="p-02-sync"),
    )

    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"
    # Still RUNNING but with a new run_id.
    assert await sm.db_state(_PR_RK) == State.RUNNING
    run_id_2 = await sm.db_run_id(_PR_RK)
    assert run_id_2 is not None
    assert run_id_2 != run_id_1
    # Two entries in the stream: original + superseding.
    assert await sm.queue_len() == 2
    # The receive side signals cancellation for the superseded run.
    assert await sm.cancel_flag(run_id_1) is True


# ── P-05: pull_request.edited → UNKNOWN kind → ignored ───────────────────


async def test_pr_edited_ignored(sm: SMHarness) -> None:
    """P-05: pull_request.edited is not in the event table → UNKNOWN → ignored."""
    body = pr_body("edited", number=7)
    resp = await sm.client.post(
        "/webhook/github",
        content=body,
        headers=sign(body, event="pull_request", delivery="p-05"),
    )

    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert await sm.queue_len() == 0
