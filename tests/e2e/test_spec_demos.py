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

from openbot.domain.events import EventKind
from openbot.infrastructure.config_loader import RateLimitConfig
from openbot.infrastructure.persistence.models import Workflow, WorkflowPhase
from openbot.infrastructure.queue import worker as queue_worker
from openbot.infrastructure.queue.enqueue import enqueue_task_spec
from openbot.infrastructure.queue.task_spec import TaskSpec
from openbot.infrastructure.queue.worker import consume_loop, ensure_consumer_group

if TYPE_CHECKING:
    from tests.e2e.conftest import WebhookHarness


# ───────────────────────── helpers ─────────────────────────


def _phases(rows: list, /) -> list[WorkflowPhase]:
    """Extract `.phase` from an ordered list of AuditLog rows."""
    return [row.phase for row in rows]


# ───────────────────────── demo 01: issue triage ack ─────────────────────────


async def test_demo_01_issue_opens_triage_acks(
    webhook_harness: WebhookHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue opens → STARTED + COMPLETED audit rows; sticky thinking → ACK.

    R4 replaced the single-reply ACK with a sticky-reply flow: a thinking
    placeholder is posted first (``reply()``), then patched with the ACK
    template (``update_comment()``) when no sandbox is present.
    """
    # Block the reproduce agent so this demo exercises the ack-only path.
    from openbot.application.use_cases import triage as triage_mod

    monkeypatch.setattr(triage_mod, "_should_run_reproduce", lambda ctx: False)
    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_OPENED,
        delivery_id="d-triage-1",
        issue_number=7,
    )
    await webhook_harness.dispatch(event)

    rows = await webhook_harness.audit_rows(delivery_id="d-triage-1")
    assert _phases(rows) == [WorkflowPhase.STARTED, WorkflowPhase.COMPLETED]
    assert all(row.workflow is Workflow.TRIAGE for row in rows)

    # Sticky-reply: thinking placeholder posted first.
    assert len(webhook_harness.adapter.replies) == 1
    repo, number, thinking_body = webhook_harness.adapter.replies[0]
    assert repo == webhook_harness.repo
    assert number == 7
    assert "reproducing this issue" in thinking_body

    # Then PATCH'd with the ACK template.
    assert len(webhook_harness.adapter.comment_updates) == 1
    update_repo, _comment_id, ack_body = webhook_harness.adapter.comment_updates[0]
    assert update_repo == webhook_harness.repo
    assert "OpenBot received this issue" in ack_body
    assert "triage shortly" in ack_body


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


# ───────────────────────── demo 03: bot-assigned fix opens a PR ───────────────


async def test_demo_03_bot_assigned_fix_opens_pr(
    webhook_harness: WebhookHarness,
) -> None:
    """Issue assigned to the bot → FIX workflow opens a PR.

    The harness monkeypatches ``_generate_fix_outcome`` so DeepAgents is
    never invoked — what we assert is the wiring around it: the sandbox
    is cloned with the raw clone URL + base SHA + installation token
    (token injection is the adapter's concern, not the use case's), a
    branch is created from the base SHA, the PR is opened with
    ``Closes #N`` in the body, and the user gets a final comment with
    the PR URL.
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

    # Sandbox was cloned with the raw clone URL + base SHA + installation
    # token. Token injection happens inside ``DaytonaSandboxAdapter`` per
    # the SandboxPort contract — the use case passes raw values through.
    assert webhook_harness.sandbox.cloned == [
        (
            "https://github.com/acme/test-repo.git",
            "abc1234567",
            "fake-installation-token",
        ),
    ]

    # Branch was created with the predictable openbot/fix-issue-N-SHORTSHA pattern.
    # The use case passes the *short* ref; the GitHub adapter adds "refs/heads/" internally.
    assert len(webhook_harness.adapter.branch_creates) == 1
    repo, branch_ref, from_sha = webhook_harness.adapter.branch_creates[0]
    assert repo == "acme/test-repo"
    assert branch_ref.startswith("openbot/fix-issue-11-")
    assert "abc1234" in branch_ref
    assert from_sha == "abc1234567"

    # Sandbox push ran between branch creation and PR open. The push
    # receives the *short* branch_ref (no ``refs/heads/`` prefix) + the
    # raw token — the adapter handles auth interpolation. Assert on the
    # full tuple so a regression in any of the three kwargs surfaces here.
    assert len(webhook_harness.sandbox.pushed) == 1
    pushed_branch, pushed_message, pushed_token = webhook_harness.sandbox.pushed[0]
    assert pushed_branch.startswith("openbot/fix-issue-11-")
    assert not pushed_branch.startswith("refs/heads/")  # short ref, not full
    assert pushed_message == "openbot: fix #11"
    assert pushed_token == "fake-installation-token"

    # PR was opened with Closes-#N in the body.
    assert len(webhook_harness.adapter.pr_creates) == 1
    pr = webhook_harness.adapter.pr_creates[0]
    assert pr["base"] == "main"
    assert pr["head"].startswith("openbot/fix-issue-11-")
    assert "Closes #11" in pr["body"]
    assert pr["title"].startswith("[OpenBot]")

    # Sticky flow: reply() posts the thinking placeholder, then update_comment()
    # delivers the final PR URL in-place.
    assert len(webhook_harness.adapter.replies) == 1  # initial placeholder
    assert len(webhook_harness.adapter.comment_updates) == 1
    _, _comment_id, body = webhook_harness.adapter.comment_updates[0]
    assert "https://github.com/acme/test-repo/pull/" in body


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
    # Sticky freeform flow: reply() posts the thinking placeholder, then
    # update_comment() delivers the actual LLM response in-place.
    assert len(webhook_harness.adapter.replies) == 1  # initial placeholder
    assert len(webhook_harness.adapter.comment_updates) == 1
    _, _comment_id, body = webhook_harness.adapter.comment_updates[0]
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

    # Build a representative TaskSpec v3 — issue.opened triage event.
    from openbot.application.router import dispatch_for

    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_OPENED,
        delivery_id="d-worker-restart",
        issue_number=21,
    )
    dispatch = dispatch_for(event)
    assert dispatch is not None, "dispatch_for returned None for ISSUE_OPENED"
    # Pre-computed classifier output so the worker takes Path (a) —
    # rehydrate without calling the LLM (no API key in e2e env).  A
    # "question" type ensures ``_triage_wants_sandbox`` returns False
    # → ``derive_sandbox_policy`` returns NO_SANDBOX → the handler
    # takes the ACK-only path without attempting to run the repro agent.
    spec = TaskSpec.from_event_and_dispatch(
        event,
        dispatch,
        initial_labels=[],
        classifier_output={
            "type": "question",
            "severity_guess": "low",
            "has_reproduction_info": False,
            "looks_like_spam": False,
        },
    )
    entry_id = await enqueue_task_spec(redis, spec)

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

    # R4 sticky-reply flow: the handler first POSTs a thinking placeholder
    # via reply(), then PATCHes it with the final ACK via update_comment()
    # when the reproduce predicate returns False (no sandbox + question-type
    # classifier output).  Both calls are recorded.
    assert len(adapter.replies) == 1
    _, number, thinking_body = adapter.replies[0]
    assert number == 21
    assert "OpenBot is reproducing this issue" in thinking_body

    assert len(adapter.comment_updates) == 1
    _, _, ack_body = adapter.comment_updates[0]
    assert "OpenBot received this issue" in ack_body

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


# ───────────────────────── demo 10: fix attempt with failing tests ──────────────


async def test_demo_10_bot_assigned_fix_tests_failed_yields_comment(
    webhook_harness: WebhookHarness,
) -> None:
    """When the (stubbed) agent reports tests_passed=False, the loop
    must comment with the truncated test output and NOT open a PR.

    This is the second observable terminal of the fix loop. Per-stage
    failure paths (clone failed, push failed, etc.) are covered by the
    use case parametrize in ``tests/application/use_cases/test_fix.py``
    — this demo carries the contract that the tests-failed terminal
    also routes correctly through the pre-flight chain + audit pipeline.
    """
    webhook_harness.fix_outcome_tests_passed = False

    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_ASSIGNED,
        delivery_id="d-fix-10",
        issue_number=22,
        raw={"assignee": {"type": "Bot", "login": "openbot[bot]"}},
    )
    await webhook_harness.dispatch(event)

    # Workflow still completed (this is a successful agent run with a
    # bad-test outcome — not a workflow error).
    rows = await webhook_harness.audit_rows(delivery_id="d-fix-10")
    assert _phases(rows) == [WorkflowPhase.STARTED, WorkflowPhase.COMPLETED]
    assert all(row.workflow is Workflow.FIX for row in rows)

    # Sandbox was cloned (with the base SHA from fake_issue) but no
    # branch/PR was attempted.
    assert webhook_harness.sandbox.cloned
    assert webhook_harness.sandbox.cloned[0][1] == "abc1234567"  # ref
    assert webhook_harness.adapter.branch_creates == []
    assert webhook_harness.adapter.pr_creates == []

    # Sticky flow: reply() posts the thinking placeholder, then update_comment()
    # delivers the tests-failed message with truncated output in-place.
    assert len(webhook_harness.adapter.replies) == 1  # initial placeholder
    assert len(webhook_harness.adapter.comment_updates) == 1
    _, _comment_id, body = webhook_harness.adapter.comment_updates[0]
    assert "tests did not pass" in body.lower()
    assert "1 failed" in body


# ─────── demo 12: sandbox cache hit < 1 s P95 (env-gated) ───────


class _FullFakeSandbox:
    """Combined sandbox: lifecycle (clone/close) + command execution (run).

    Used by demo 12 so a single sandbox instance can:
      - serve the cold-path factory (clone + close),
      - be stored in InMemorySandboxCache (run via _refresh_to_ref).
    """

    def __init__(self) -> None:
        from openbot.application.ports.sandbox import ExecResult

        self._ok = ExecResult(stdout="", stderr="", exit_code=0, timed_out=False)
        self.cloned: list[tuple[str, str, str]] = []
        self.calls: list[list[str]] = []
        self.closed: bool = False

    async def clone(self, *, repo_url: str, ref: str, token: str, strategy: object = None) -> None:
        self.cloned.append((repo_url, ref, token))

    async def run(
        self,
        *,
        command: list[str],
        env: object = None,
        timeout_seconds: int = 60,
    ) -> object:
        self.calls.append(command)
        return self._ok

    async def close(self) -> None:
        self.closed = True


@pytest.mark.skipif(
    not __import__("os").getenv("RUN_CACHE_E2E"),
    reason="env-gated; run with RUN_CACHE_E2E=1 to opt in",
)
async def test_demo_12_chat_cache_hit_under_one_second(
    webhook_harness: WebhookHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second chat event with same (repo, ref) resolves from InMemorySandboxCache
    in < 1 s wall-clock, proving the cache path avoids cold-clone overhead.

    Structure:
      1. Wire InMemorySandboxCache + _FullFakeSandbox factory into the harness.
      2. Replace sandbox_cache_total with a MagicMock so we can assert on calls.
      3. First dispatch  → cold path → clone recorded → cache publish scheduled.
      4. Drain asyncio event loop so background publish task runs.
      5. Second dispatch → cache hit → no clone → elapsed < 1 s.
      6. Assert sandbox_cache_total.labels called with feature=chat, result=hit.
    """
    import time
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    from openbot.infrastructure.sandboxes.cache_fake import InMemorySandboxCache

    # Replace the Prometheus wrapper with a MagicMock (same pattern as
    # tests/application/test_dispatcher_cache.py::_record_counter).
    cache_total_mock = MagicMock()
    monkeypatch.setattr("openbot.application.dispatcher.sandbox_cache_total", cache_total_mock)

    cache = InMemorySandboxCache(max_entries=10, ttl_seconds=86_400)
    fake_sandbox = _FullFakeSandbox()

    @asynccontextmanager
    async def _factory():  # type: ignore[return]
        try:
            yield fake_sandbox
        finally:
            await fake_sandbox.close()

    webhook_harness.sandbox_factory_override = _factory
    webhook_harness.sandbox_cache_override = cache

    event = webhook_harness.make_event(
        kind=EventKind.ISSUE_COMMENT_CREATED,
        delivery_id="d-cache-demo-12-cold",
        issue_number=12,
        comment_body="@openbot explain this code",
    )

    # ── First dispatch: cold path ────────────────────────────────────────────
    await webhook_harness.dispatch(event)
    # Drain the event loop so the background _safe_publish task completes.
    await asyncio.sleep(0.05)

    # Cold path must have cloned once.
    assert len(fake_sandbox.cloned) == 1, "expected one clone on cold path"
    # Publish must have run — exactly one entry in the cache.
    assert cache.size() == 1, "expected cache entry after publish"

    # ── Second dispatch: warm cache hit ──────────────────────────────────────
    event2 = webhook_harness.make_event(
        kind=EventKind.ISSUE_COMMENT_CREATED,
        delivery_id="d-cache-demo-12-warm",
        issue_number=12,
        comment_body="@openbot what does this function do",
    )

    t_start = time.perf_counter()
    await webhook_harness.dispatch(event2)
    elapsed = time.perf_counter() - t_start

    # In-memory cache with FakeSandbox is effectively instant.
    assert elapsed < 1.0, f"cache hit took {elapsed:.3f}s; expected < 1s"

    # Hit counter must be emitted exactly once for feature=chat, result=hit.
    cache_total_mock.labels.assert_any_call(feature="chat", result="hit")

    # No second clone on the warm path.
    assert len(fake_sandbox.cloned) == 1, "expected no clone on cache hit"
