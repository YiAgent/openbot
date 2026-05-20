# Webhook-Worker Layering F2 — Implementation Plan (Part 2: Tasks 5–6)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire D11+D12 into `decide_and_enqueue()` so the direct-action short-circuit runs between D2-D9 preflight and the TaskSpec enqueue. Add an end-to-end acceptance test (F-02) that confirms all three scenarios (empty body, oversized PR, vague mention).

**Architecture:** Minimal change to `openbot/dispatcher/decide.py` — insert D11 (`extract_event_context`) and D12 (feature-dispatched rule call) after `run_preflight` returns PROCEED. If a `DirectAction` fires: reply, optionally add label, return without enqueuing. Otherwise fall through to existing TaskSpec / in-process path.

**Tech Stack:** Python 3.12, pytest-asyncio, FakeChannelAdapter (from Task 2), FakeQueue.

---

### Task 5: Wire D11+D12 into `decide_and_enqueue()`

**Files:**
- Modify: `openbot/dispatcher/decide.py`
- Test: `tests/application/dispatcher/test_decide_direct_actions.py` (new)

This is the integration step: `decide_and_enqueue` already runs D1-D9 (config load + preflight). After PROCEED, we inject D11 (context extraction) and D12 (rule evaluation). If a DirectAction fires, we reply via the adapter, optionally add labels, log, and return early without building a TaskSpec.

- [ ] **Step 1: Write integration tests first**

Create `tests/application/dispatcher/test_decide_direct_actions.py`:

```python
# tests/application/dispatcher/test_decide_direct_actions.py
"""Integration tests: decide_and_enqueue() direct-action short-circuit (D11+D12)."""
from __future__ import annotations

import pytest

from openbot.dispatcher import decide_and_enqueue
from openbot.domain.events import EventKind, UnifiedEvent
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests._fakes.queue import FakeQueue

# Re-use the make_event helper from the middleware conftest
from tests.application.middleware.conftest import make_event


def _pr_event(additions: int, deletions: int = 0) -> UnifiedEvent:
    """Build a PR_OPENED event with realistic raw payload."""
    return UnifiedEvent(
        channel="github",
        delivery_id="del-pr",
        kind=EventKind.PR_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        issue_number=None,
        pr_number=42,
        installation_id=100,
        comment_body=None,
        raw={
            "pull_request": {
                "number": 42,
                "additions": additions,
                "deletions": deletions,
                "changed_files": 10,
            }
        },
    )


def _issue_event(body: str | None, *, body_key_absent: bool = False) -> UnifiedEvent:
    """Build an ISSUE_OPENED event. body_key_absent=True omits the body key."""
    issue: dict = {"number": 7, "labels": []}
    if not body_key_absent:
        issue["body"] = body
    return UnifiedEvent(
        channel="github",
        delivery_id="del-issue",
        kind=EventKind.ISSUE_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        issue_number=7,
        pr_number=None,
        installation_id=100,
        comment_body=None,
        raw={"issue": issue},
    )


def _mention_event(comment: str) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="del-mention",
        kind=EventKind.ISSUE_COMMENT,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        issue_number=7,
        pr_number=None,
        installation_id=100,
        comment_body=comment,
        raw={},
    )


@pytest.mark.asyncio
async def test_empty_body_sends_reply_and_adds_label_no_enqueue() -> None:
    """Empty issue body → reply with needs-info message, add label, no TaskSpec."""
    from openbot.application.router import dispatch_for

    event = _issue_event(body="")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    adapter = FakeChannelAdapter()
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    # No TaskSpec enqueued
    assert len(queue.task_specs) == 0
    # A reply was sent
    assert len(adapter.replies) == 1
    assert "needs-info" in adapter.replies[0][1].lower() or "empty" in adapter.replies[0][1].lower()
    # The needs-info label was applied
    assert len(adapter.labels_added) == 1
    assert "needs-info" in adapter.labels_added[0][1]


@pytest.mark.asyncio
async def test_body_present_continues_to_enqueue() -> None:
    """Non-empty body → no direct action, normal enqueue path."""
    from openbot.application.router import dispatch_for

    event = _issue_event(body="This is a detailed issue description about the auth bug.")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    adapter = FakeChannelAdapter()
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    assert len(queue.task_specs) == 1
    assert len(adapter.replies) == 0
    assert len(adapter.labels_added) == 0


@pytest.mark.asyncio
async def test_oversized_pr_sends_reply_no_enqueue() -> None:
    """PR with > 500 total line changes → split suggestion reply, no TaskSpec."""
    from openbot.application.router import dispatch_for

    event = _pr_event(additions=400, deletions=200)  # 600 total
    dispatch = dispatch_for(event)
    assert dispatch is not None

    adapter = FakeChannelAdapter()
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    assert len(queue.task_specs) == 0
    assert len(adapter.replies) == 1
    assert "600" in adapter.replies[0][1]


@pytest.mark.asyncio
async def test_normal_pr_size_continues_to_enqueue() -> None:
    """PR within threshold → no direct action."""
    from openbot.application.router import dispatch_for

    event = _pr_event(additions=100, deletions=50)  # 150 total, under 500
    dispatch = dispatch_for(event)
    assert dispatch is not None

    adapter = FakeChannelAdapter()
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    assert len(queue.task_specs) == 1
    assert len(adapter.replies) == 0


@pytest.mark.asyncio
async def test_vague_mention_sends_clarification_no_enqueue() -> None:
    """Short/empty @mention → clarification reply, no TaskSpec."""
    from openbot.application.router import dispatch_for

    event = _mention_event("@openbot hi")
    dispatch = dispatch_for(event)
    # ISSUE_COMMENT may not map to CHAT dispatch in all configs;
    # skip if dispatch is None (router not configured for this event kind)
    if dispatch is None:
        pytest.skip("ISSUE_COMMENT not routed to CHAT in current config")

    adapter = FakeChannelAdapter()
    queue = FakeQueue()

    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=queue,
        session_factory=None,
        redis=None,
    )

    assert len(queue.task_specs) == 0
    assert len(adapter.replies) == 1


@pytest.mark.asyncio
async def test_direct_action_reply_failure_swallowed() -> None:
    """If adapter.reply() raises, decide_and_enqueue must NOT propagate."""
    from openbot.application.router import dispatch_for
    from unittest.mock import AsyncMock

    event = _issue_event(body="")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    adapter = FakeChannelAdapter()
    adapter.reply = AsyncMock(side_effect=RuntimeError("GitHub API down"))

    # Should not raise
    await decide_and_enqueue(
        adapter=adapter,
        event=event,
        dispatch=dispatch,
        config_loader=FakeConfigLoader(),
        queue=FakeQueue(),
        session_factory=None,
        redis=None,
    )
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
cd /Users/wy/projects/openbot/.worktrees/feat-webhook-worker-layering-f2
python -m pytest tests/application/dispatcher/test_decide_direct_actions.py -v 2>&1 | tail -30
```

Expected: tests for empty body and oversized PR fail (no direct action yet in decide.py). The `test_direct_action_reply_failure_swallowed` test may pass vacuously since no reply is sent yet.

- [ ] **Step 3: Read `decide.py` then add D11+D12**

Read `openbot/dispatcher/decide.py`. After the line `if decision.result is not MiddlewareResult.PROCEED: return`, add the D11+D12 block:

```python
        # D11: Extract structured context from raw payload (pure, no I/O).
        from openbot.dispatcher.context import extract_event_context
        from openbot.dispatcher.direct_actions import (
            check_issue_completeness,
            check_mention_clarity,
            check_pr_size,
        )
        from openbot.domain.workflows import Feature

        ev_ctx = extract_event_context(event)
        feature = dispatch.feature
        direct_action = (
            check_issue_completeness(ev_ctx)
            if feature is Feature.TRIAGE
            else check_pr_size(ev_ctx)
            if feature is Feature.REVIEW
            else check_mention_clarity(ev_ctx)
            if feature is Feature.CHAT
            else None
        )

        # D12: Short-circuit — reply and return without enqueuing.
        if direct_action is not None:
            try:
                await adapter.reply(event, direct_action.message)
                if direct_action.labels_to_add:
                    await adapter.add_label(event, *direct_action.labels_to_add)
            except Exception:
                _logger.exception(
                    "direct_action_reply_failed",
                    extra={"delivery_id": event.delivery_id, "repo": event.repo},
                )
            _logger.info(
                "decide_and_enqueue_direct_action",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    "feature": str(feature),
                    "labels_added": direct_action.labels_to_add,
                },
            )
            return
```

The full updated flow in `decide_and_enqueue` is:
1. D1: config load
2. D2-D9: `run_preflight` → `decision`
3. If not PROCEED → return
4. **D11**: `extract_event_context(event)` → `ev_ctx`
5. **D12**: feature-dispatched rule call → `direct_action`
6. If `direct_action` is not None → reply + add_label + return
7. Else → build TaskSpec or in-process fallback (existing code unchanged)

- [ ] **Step 4: Run integration tests to confirm PASS**

```bash
python -m pytest tests/application/dispatcher/test_decide_direct_actions.py -v 2>&1 | tail -30
```

Expected: all 6 tests pass (or 5 if `ISSUE_COMMENT` isn't routed to CHAT, which would skip that one).

- [ ] **Step 5: Confirm existing decide tests still pass**

```bash
python -m pytest tests/application/dispatcher/ -v 2>&1 | tail -30
```

Expected: all previously-passing tests still pass.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add openbot/dispatcher/decide.py tests/application/dispatcher/test_decide_direct_actions.py
git commit -m "feat(dispatcher): wire D11+D12 direct-action short-circuit into decide_and_enqueue"
```

---

### Task 6: Final Check — `make check` and F-02 Acceptance Verification

**Files:**
- No new files

This task verifies the complete F2 implementation passes all quality gates (fmt, lint, tests) and documents the F-02 acceptance criteria met.

- [ ] **Step 1: Run `make check`**

```bash
cd /Users/wy/projects/openbot/.worktrees/feat-webhook-worker-layering-f2
make check 2>&1 | tail -30
```

Expected: `fmt-check` passes, `lint` passes, all tests pass.

If `fmt-check` fails, run `make fmt` first then re-run `make check`.

- [ ] **Step 2: Verify F-02 acceptance criteria**

Run the acceptance test suite to confirm all three direct-action scenarios work:

```bash
python -m pytest tests/application/dispatcher/test_decide_direct_actions.py -v 2>&1
```

Expected output (all PASS):
```
tests/application/dispatcher/test_decide_direct_actions.py::test_empty_body_sends_reply_and_adds_label_no_enqueue PASSED
tests/application/dispatcher/test_decide_direct_actions.py::test_body_present_continues_to_enqueue PASSED
tests/application/dispatcher/test_decide_direct_actions.py::test_oversized_pr_sends_reply_no_enqueue PASSED
tests/application/dispatcher/test_decide_direct_actions.py::test_normal_pr_size_continues_to_enqueue PASSED
tests/application/dispatcher/test_decide_direct_actions.py::test_vague_mention_sends_clarification_no_enqueue PASSED (or SKIPPED)
tests/application/dispatcher/test_decide_direct_actions.py::test_direct_action_reply_failure_swallowed PASSED
```

- [ ] **Step 3: Count total tests and confirm growth**

```bash
python -m pytest --co -q 2>&1 | tail -5
```

Expected: test count should be ≥ 730 + new tests from Tasks 1-5 (approximately 742+).

- [ ] **Step 4: Commit final state**

```bash
git add -A
git commit -m "chore: final F2 verification — make check green, 742+ tests passing"
```

---

**F2 complete.** The implementation is ready to push and create a PR via `superpowers:finishing-a-development-branch`.

## F2 Feature Summary

| Component | File | Purpose |
|-----------|------|---------|
| Classifier fix | `openbot/application/state/classifier.py` | UNLABELED events → IGNORE/label_removed (§6.1 必做) |
| Port extension | `openbot/application/ports/channel_adapter.py` | `add_label` on Protocol |
| Fake update | `tests/_fakes/channel_adapter.py` | `add_label` + `labels_added` tracking |
| Context extraction | `openbot/dispatcher/context.py` | D11 — pure `EventContext` from `event.raw` |
| Rule functions | `openbot/dispatcher/direct_actions.py` | D12 — 3 pure rules returning `DirectAction | None` |
| Wiring | `openbot/dispatcher/decide.py` | D11+D12 inserted after preflight PROCEED |
