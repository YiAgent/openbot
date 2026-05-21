"""Spec §7 demos — end-to-end acceptance tests.

These nine tests cover docs/_archive/webhook-worker/openbot-harness-spec.md
§7 demos 1-9 (spec archived 2026-05-20; all demos green).
Each runs the full pre-flight chain + workflow handler against an
in-memory stack (sqlite + fakeredis + RecordingGitHubAdapter) via the
``webhook_harness`` fixture in ``conftest.py``.

The webhook signature + queue serialization path is already covered by
``tests/test_webhook_endpoint.py`` and ``tests/test_queue_worker.py``;
demos 1-8 call ``WebhookHarness.dispatch`` directly with a synthetic
``UnifiedEvent``. Only demo 9 exercises ``consume_loop`` because it has
to assert PEL → XAUTOCLAIM recovery semantics, which only the queue
worker provides.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from openbot.application.router import derive_task_id
from openbot.domain.events import EventKind
from openbot.infrastructure.config_loader import RateLimitConfig
from openbot.infrastructure.llm.model_router import Feature
from openbot.infrastructure.persistence.models import Workflow, WorkflowPhase
from openbot.infrastructure.queue import worker as queue_worker
from openbot.infrastructure.queue.enqueue import enqueue
from openbot.infrastructure.queue.payload import QueuePayload
from openbot.infrastructure.queue.worker import consume_loop, ensure_consumer_group

if TYPE_CHECKING:
    from tests.e2e.conftest import WebhookHarness


# ───────────────────────── helpers ─────────────────────────


def _phases(rows: list, /) -> list[WorkflowPhase]:
    """Extract `.phase` from an ordered list of AuditLog rows."""
    return [row.phase for row in rows]


# ───────────────────────── demo 01: issue triage ack ─────────────────────────


async def test_demo_01_issue_opens_triage_acks(webhook_harness: WebhookHarness) -> None:
    """Issue opens → STARTED + COMPLETED audit rows; one triage ACK reply."""
    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_OPENED,
        delivery_id="d-triage-1",
        issue_number=7,
    )
    await webhook_harness.dispatch(event)

    rows = await webhook_harness.audit_rows(delivery_id="d-triage-1")
    assert _phases(rows) == [WorkflowPhase.STARTED, WorkflowPhase.COMPLETED]
    assert all(row.workflow is Workflow.TRIAGE for row in rows)
    assert len(webhook_harness.adapter.replies) == 1
    repo, number, body = webhook_harness.adapter.replies[0]
    assert repo == webhook_harness.repo
    assert number == 7
    assert "OpenBot received this issue" in body


# ───────────────────────── demo 02: PR review stub ack ─────────────────────────


async def test_demo_02_pr_opens_review_stub_acks(webhook_harness: WebhookHarness) -> None:
    """PR opens (same-repo, not a fork) → REVIEW workflow ACK."""
    event = webhook_harness.make_event(
        kind=EventKind.PR_OPENED,
        delivery_id="d-review-1",
        issue_number=None,
        pr_number=42,
        # Same-repo PR — ForkPRGate sees head.full_name == base.full_name.
        raw={
            "pull_request": {
                "head": {"repo": {"full_name": webhook_harness.repo, "fork": False}},
                "base": {"repo": {"full_name": webhook_harness.repo}},
            }
        },
    )
    await webhook_harness.dispatch(event)

    rows = await webhook_harness.audit_rows(delivery_id="d-review-1")
    assert _phases(rows) == [WorkflowPhase.STARTED, WorkflowPhase.COMPLETED]
    assert all(row.workflow is Workflow.REVIEW for row in rows)
    # Slice B: review goes through the PR Review API, not a free-form comment.
    # The fake responder in conftest emits zero findings, so we expect a single
    # APPROVE review with the summary as the body.
    assert len(webhook_harness.adapter.pr_reviews) == 1
    review = webhook_harness.adapter.pr_reviews[0]
    assert review["pr_number"] == 42
    assert review["event_type"] == "APPROVE"
    assert review["body"] == "DeepAgents review summary for PR #42"
    assert review["comments"] == []
    # Free-form `reply()` is no longer the review path — this also acts as a
    # regression guard against accidentally re-routing through `issues/.../comments`.
    assert webhook_harness.adapter.replies == []


# ───────────────────────── demo 03: bot-assigned fix stub ─────────────────────────


async def test_demo_03_bot_assigned_fix_stub(webhook_harness: WebhookHarness) -> None:
    """Issue assigned to the bot → FIX workflow ACK.

    Router gates fix on ``assignee.type == "Bot"``. ActorRoleMiddleware
    then checks the *actor's* role against the FIX allow-list; the
    RecordingGitHubAdapter defaults actor_role to "admin" so the gate
    passes without further setup.
    """
    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_ASSIGNED,
        delivery_id="d-fix-1",
        issue_number=11,
        raw={"assignee": {"type": "Bot", "login": "openbot[bot]"}},
    )
    await webhook_harness.dispatch(event)

    rows = await webhook_harness.audit_rows(delivery_id="d-fix-1")
    assert _phases(rows) == [WorkflowPhase.STARTED, WorkflowPhase.COMPLETED]
    assert all(row.workflow is Workflow.FIX for row in rows)
    assert len(webhook_harness.adapter.replies) == 1
    _, number, body = webhook_harness.adapter.replies[0]
    assert number == 11
    assert "fix assignment on issue #11" in body


# ───────────────────────── demo 04: @openbot chat ack ─────────────────────────


async def test_demo_04_at_openbot_chat_ack(webhook_harness: WebhookHarness) -> None:
    """`@openbot tell me about X` (freeform, not cancel/help) → CHAT reply.

    The e2e harness stubs the DeepAgents call itself; this test verifies
    the workflow path switched from the old canned ACK to the real
    freeform branch.
    """
    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_COMMENT_CREATED,
        delivery_id="d-chat-1",
        issue_number=5,
        comment_body="@openbot tell me about this repo",
    )
    await webhook_harness.dispatch(event)

    rows = await webhook_harness.audit_rows(delivery_id="d-chat-1")
    assert _phases(rows) == [WorkflowPhase.STARTED, WorkflowPhase.COMPLETED]
    assert all(row.workflow is Workflow.CHAT for row in rows)
    assert len(webhook_harness.adapter.replies) == 1
    _, number, body = webhook_harness.adapter.replies[0]
    assert number == 5
    assert body == "DeepAgents test reply: tell me about this repo"


# ───────────────────────── demo 05: cancel label blocks ─────────────────────────


async def test_demo_05_cancel_label_blocks(webhook_harness: WebhookHarness) -> None:
    """Issue carries cancel-openbot label → BLOCKED at cancel_label gate.

    The middleware writes a REJECTED audit row with outcome="cancel_label"
    and posts NO comment (the user already signaled intent by labeling).
    No workflow STARTED/COMPLETED rows because the chain short-circuits
    before AuditStartMiddleware.
    """
    # CancelLabelMiddleware fetches labels via _authed_json; the harness
    # adapter returns whatever we put on `labels_response`.
    webhook_harness.adapter.labels_response = [
        {"name": webhook_harness.config.cancel.label},  # "cancel-openbot"
    ]

    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_OPENED,
        delivery_id="d-cancel-label-1",
        issue_number=99,
    )
    await webhook_harness.dispatch(event)

    rows = await webhook_harness.audit_rows(delivery_id="d-cancel-label-1")
    # One REJECTED row (from preflight) — no STARTED/COMPLETED because
    # the chain short-circuited before AuditStartMiddleware.
    assert _phases(rows) == [WorkflowPhase.REJECTED]
    assert rows[0].outcome == "cancel_label"
    assert rows[0].workflow is Workflow.TRIAGE
    # cancel_label is intentional drop — no reply.
    assert webhook_harness.adapter.replies == []


# ───────────────────────── demo 06: kill switch env blocks ─────────────────────────


async def test_demo_06_kill_switch_env_blocks(
    webhook_harness: WebhookHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPENBOT_KILL_SWITCH=true → REJECTED at kill_switch (no comment)."""
    monkeypatch.setenv("OPENBOT_KILL_SWITCH", "true")

    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_OPENED,
        delivery_id="d-kill-1",
        issue_number=3,
    )
    await webhook_harness.dispatch(event)

    rows = await webhook_harness.audit_rows(delivery_id="d-kill-1")
    assert _phases(rows) == [WorkflowPhase.REJECTED]
    assert rows[0].outcome == "kill_switch"
    # Kill switch == intentional silence; no GitHub egress.
    assert webhook_harness.adapter.replies == []


# ───────────────────────── demo 07: fork PR ok-to-test evolution ─────────────────


async def test_demo_07_fork_pr_default_off_ok_to_test_opens(
    webhook_harness: WebhookHarness,
) -> None:
    """Fork PR: first event BLOCKED + announce; after /ok-to-test, PROCEED.

    Step 1 — fork PR arrives. config.fork_pr.run defaults False, no
    maintainer comment yet → BLOCKED with SKIPPED phase + announce_key.
    One reply (the announce_once notice).

    Step 2 — same PR (different delivery_id), this time the comments
    feed contains /ok-to-test from a maintainer. The gate PROCEEDs and
    the review workflow runs to COMPLETED.
    """
    # Step 1: fork PR, no /ok-to-test yet.
    webhook_harness.adapter.comments_response = []  # no maintainer comment
    # Adapter default role is "admin" which is in _MAINTAINER_ROLES; the
    # gate only consults role when there's a candidate comment. For the
    # blocked path we just need an empty comment list.

    fork_pr_raw = {
        "pull_request": {
            "head": {
                "repo": {"full_name": "outsider/test-repo-fork", "fork": True},
            },
            "base": {"repo": {"full_name": webhook_harness.repo}},
        }
    }

    event_blocked = webhook_harness.make_event(
        kind=EventKind.PR_OPENED,
        delivery_id="d-fork-blocked",
        issue_number=None,
        pr_number=77,
        actor="outside-contributor",
        raw=fork_pr_raw,
    )
    await webhook_harness.dispatch(event_blocked)

    blocked_rows = await webhook_harness.audit_rows(delivery_id="d-fork-blocked")
    assert _phases(blocked_rows) == [WorkflowPhase.SKIPPED]
    assert blocked_rows[0].outcome == "fork_pr_no_ok_to_test"
    # announce_once posted the "fork PRs are off by default" notice once.
    assert len(webhook_harness.adapter.replies) == 1
    assert "fork PRs by default" in webhook_harness.adapter.replies[0][2]

    # Step 2: same PR, this time a maintainer has /ok-to-test'd it.
    webhook_harness.adapter.comments_response = [
        {"body": "/ok-to-test", "user": {"login": "maintainer-alice"}},
    ]
    webhook_harness.adapter.actor_roles["maintainer-alice"] = "admin"

    event_ok = webhook_harness.make_event(
        kind=EventKind.PR_OPENED,
        delivery_id="d-fork-ok",
        issue_number=None,
        pr_number=77,
        actor="outside-contributor",
        raw=fork_pr_raw,
    )
    await webhook_harness.dispatch(event_ok)

    ok_rows = await webhook_harness.audit_rows(delivery_id="d-fork-ok")
    assert _phases(ok_rows) == [WorkflowPhase.STARTED, WorkflowPhase.COMPLETED]
    assert all(row.workflow is Workflow.REVIEW for row in ok_rows)
    # Step 1 posted 1 reply (the BLOCK notice via `reply`); Step 2 now goes
    # through the PR Review API (slice B), so `replies` stays at 1 and
    # `pr_reviews` gets a new entry.
    assert len(webhook_harness.adapter.replies) == 1
    assert len(webhook_harness.adapter.pr_reviews) == 1
    assert webhook_harness.adapter.pr_reviews[0]["pr_number"] == 77


# ───────────────────────── demo 08: rate limit announce_once ─────────────────────


async def test_demo_08_rate_limited_user_sees_single_comment(
    webhook_harness: WebhookHarness,
) -> None:
    """N + 2 chat requests → N ACKs + 1 rate-limit notice (announce_once).

    Threshold dialed to per_user_per_day=2 so the test runs in a handful
    of iterations instead of 21. Behaviour is identical to the spec
    demo: requests over the threshold share the same announce_key, so
    only the first BLOCKED reply surfaces.
    """
    # Replace the rate limit section with a low threshold so we don't
    # have to fire 21 dispatches. `replace` returns a new frozen config.
    low_threshold = RateLimitConfig(
        per_user_per_day=2,
        per_repo_per_hour=100,
        cost_cap_per_task=Decimal("0.30"),
        # Drop exempt roles so the "alice/admin" default doesn't bypass.
        exempt_roles=frozenset(),
    )
    webhook_harness.config = replace(webhook_harness.config, rate_limit=low_threshold)

    # Send 4 chat requests from the same actor. 1-2 PROCEED, 3-4 BLOCKED
    # with the same announce_key → only request 3's comment is sent.
    for i in range(4):
        event = webhook_harness.make_event(
            kind=EventKind.ISSUE_COMMENT_CREATED,
            delivery_id=f"d-rl-{i}",
            issue_number=8,
            comment_body=f"@openbot question {i}",
        )
        await webhook_harness.dispatch(event)

    # 2 chat ACK replies (i=0, 1) + 1 rate-limit notice (i=2; i=3 dedup'd).
    assert len(webhook_harness.adapter.replies) == 3
    rate_limit_bodies = [
        body for _, _, body in webhook_harness.adapter.replies if "Rate limited" in body
    ]
    assert len(rate_limit_bodies) == 1
    # Audit log: 2 STARTED+COMPLETED pairs, then 2 SKIPPED rows for the
    # blocked attempts.
    all_rows = await webhook_harness.audit_rows()
    assert _phases(all_rows).count(WorkflowPhase.STARTED) == 2
    assert _phases(all_rows).count(WorkflowPhase.COMPLETED) == 2
    assert _phases(all_rows).count(WorkflowPhase.SKIPPED) == 2


# ───────────────────────── demo 09: worker restart preserves message ────────────


async def test_demo_09_worker_restart_does_not_drop_message(
    webhook_harness: WebhookHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crashed consumer (entry in PEL) → restarted worker XAUTOCLAIMs it.

    Sequence:
      1. XADD a payload to the workflows stream.
      2. XREADGROUP with consumer "dead-c1" → entry moves into PEL but is
         never XACK'd (simulating the consumer crashing mid-handler).
      3. Monkeypatch ``_PENDING_IDLE_MS`` to 0 so XAUTOCLAIM reclaims
         the entry immediately (rather than after the 60s production
         default).
      4. Run a single ``consume_loop`` iteration with consumer "c2";
         it should XAUTOCLAIM + dispatch the payload exactly once.
      5. Assert the workflow handler ran (one reply, one STARTED+
         COMPLETED pair) and the PEL is now empty.
    """
    # Reclaim instantly so we don't sit on the 60-second production
    # idle timer. `_PENDING_IDLE_MS` is a module Final — Final is a
    # type-checker hint only, runtime mutation is allowed.
    monkeypatch.setattr(queue_worker, "_PENDING_IDLE_MS", 0)

    redis = webhook_harness.redis
    adapter = webhook_harness.adapter
    session_factory = webhook_harness.session_factory

    await ensure_consumer_group(redis)

    # Build a representative payload — issue.opened triage event.
    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_OPENED,
        delivery_id="d-worker-restart",
        issue_number=21,
    )
    payload = QueuePayload.from_event(
        event,
        feature=Feature.TRIAGE,
        task_id=derive_task_id(event),
    )
    entry_id = await enqueue(redis, payload)

    # Simulate the "first consumer crashed mid-handler": read the entry
    # under a dead consumer name without XACKing. The entry now sits in
    # the consumer group's PEL forever (in production: until idle >
    # _PENDING_IDLE_MS, at which point another consumer reclaims).
    crashed_read = await redis.xreadgroup(
        groupname="openbot:workflows:group",
        consumername="dead-c1",
        streams={"openbot:workflows": ">"},
        count=10,
        block=10,
    )
    assert crashed_read, "expected to read the just-XADD'd entry under dead consumer"

    # No XACK — leave it in PEL.

    # Now spin up a second consumer with a short XREADGROUP block so the
    # one-iteration shutdown takes effect quickly.
    shutdown = asyncio.Event()

    async def run_one_iteration() -> None:
        await consume_loop(
            redis=redis,
            adapter=adapter,
            session_factory=session_factory,
            consumer_name="c2",
            shutdown=shutdown,
            read_block_ms=50,
        )

    consumer_task = asyncio.create_task(run_one_iteration())
    # Give the loop time to do one XAUTOCLAIM + dispatch + XACK cycle.
    # Each iteration: reclaim (instant, idle=0), read (blocks up to 50ms
    # since the stream has no more new entries), then loops. Two cycles
    # is plenty.
    await asyncio.sleep(0.3)
    shutdown.set()
    try:
        await asyncio.wait_for(consumer_task, timeout=2.0)
    except TimeoutError:  # pragma: no cover — defensive
        consumer_task.cancel()
        raise

    # The triage workflow should have run exactly once.
    assert len(adapter.replies) == 1
    _, number, body = adapter.replies[0]
    assert number == 21
    assert "OpenBot received this issue" in body

    # Audit log: one STARTED+COMPLETED pair for the single dispatch.
    rows = await webhook_harness.audit_rows(delivery_id="d-worker-restart")
    assert _phases(rows) == [WorkflowPhase.STARTED, WorkflowPhase.COMPLETED]

    # PEL is now empty — the reclaim + XACK cleared it.
    pending = await redis.xpending("openbot:workflows", "openbot:workflows:group")
    # Different redis-py versions return either a dict or a tuple-of-4.
    pending_count = pending["pending"] if isinstance(pending, dict) else pending[0]
    assert int(pending_count) == 0, (
        f"expected empty PEL after reclaim+XACK, got pending={pending} (entry_id={entry_id})"
    )
