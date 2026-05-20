# Webhook-Worker Layering F3 — Plan (Part 1: Tasks 1–2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal (overall F3):** Add D10 LLM classifier + incremental PR review scope to `decide_and_enqueue()`. F3 delivers `stages_to_run` populated from classifier output and `is_incremental`/`is_force_push` flags in TaskSpec.

**Part 1 scope:** Extend TaskSpec (Task 1) + create `incremental.py` (Task 2). See Part 2 for `classifier.py`, Part 3 for wiring + acceptance.

**Tech Stack:** Python 3.12, pytest-asyncio, frozen dataclasses.

---

### Task 1: Extend TaskSpec with F3 fields

**Files:**
- Modify: `openbot/infrastructure/queue/task_spec.py`
- Test: `tests/infrastructure/queue/test_task_spec_f3_fields.py` (new)

Three new optional fields at the end of `TaskSpec`. All have defaults so old v3 JSON still deserialises cleanly.

- [ ] **Step 1: Write failing tests**

Create `tests/infrastructure/queue/test_task_spec_f3_fields.py`:

```python
# tests/infrastructure/queue/test_task_spec_f3_fields.py
"""TaskSpec F3 extension: classifier_output, is_incremental, is_force_push."""
from __future__ import annotations

import json

from openbot.infrastructure.queue.task_spec import TaskSpec, deserialize_task_spec


def _base_kwargs() -> dict:
    return dict(
        spec_version=3, task_id="t1", run_id="r1", prev_run_id=None,
        resource_key="github:org/repo:issue:1", event_seq=0, intent="start",
        enqueued_at="2026-01-01T00:00:00+00:00",
        spec_built_at="2026-01-01T00:00:00+00:00",
        scenario="triage", channel="github", delivery_id="del-1",
        kind="issue.opened", repo="org/repo", actor="alice",
        actor_type="User", issue_number=1, pr_number=None,
        comment_body=None, installation_id=100, raw={},
        check_run_id=None, decision_trace=[],
        classifier_skipped=True, stages_to_run=[], initial_labels=[],
    )


def test_new_fields_default_to_none_false() -> None:
    spec = TaskSpec(**_base_kwargs())
    assert spec.classifier_output is None
    assert spec.is_incremental is False
    assert spec.is_force_push is False


def test_new_fields_roundtrip() -> None:
    spec = TaskSpec(
        **_base_kwargs(),
        classifier_output={"type": "bug"},
        is_incremental=True,
        is_force_push=False,
    )
    restored = deserialize_task_spec(spec.to_json())
    assert restored is not None
    assert restored.classifier_output == {"type": "bug"}
    assert restored.is_incremental is True


def test_old_v3_json_without_new_fields() -> None:
    """Old v3 JSON missing new keys → defaults applied (backward-compat)."""
    blob = json.dumps(_base_kwargs())
    spec = deserialize_task_spec(blob)
    assert spec is not None
    assert spec.classifier_output is None
    assert spec.is_incremental is False


def test_from_event_and_dispatch_defaults() -> None:
    from openbot.domain.events import EventKind, UnifiedEvent
    from openbot.application.router import dispatch_for

    event = UnifiedEvent(
        channel="github", delivery_id="del-2", kind=EventKind.ISSUE_OPENED,
        repo="org/repo", actor="alice", actor_type="User", issue_number=5,
        pr_number=None, installation_id=100, comment_body=None,
        raw={"issue": {"number": 5, "labels": []}},
    )
    dispatch = dispatch_for(event)
    assert dispatch is not None

    spec = TaskSpec.from_event_and_dispatch(event, dispatch)
    assert spec.classifier_output is None
    assert spec.is_incremental is False
    assert spec.classifier_skipped is True   # no output → skipped


def test_from_event_and_dispatch_with_classifier_output() -> None:
    from openbot.domain.events import EventKind, UnifiedEvent
    from openbot.application.router import dispatch_for

    event = UnifiedEvent(
        channel="github", delivery_id="del-3", kind=EventKind.ISSUE_OPENED,
        repo="org/repo", actor="alice", actor_type="User", issue_number=6,
        pr_number=None, installation_id=100, comment_body=None,
        raw={"issue": {"number": 6, "labels": []}},
    )
    dispatch = dispatch_for(event)
    assert dispatch is not None

    output = {"type": "feature", "severity_guess": "low"}
    spec = TaskSpec.from_event_and_dispatch(
        event, dispatch,
        classifier_output=output,
        is_incremental=True,
        is_force_push=False,
    )
    assert spec.classifier_output == output
    assert spec.is_incremental is True
    assert spec.classifier_skipped is False   # output present → not skipped
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
cd /Users/wy/projects/openbot/.worktrees/feat-webhook-worker-layering-f2
python -m pytest tests/infrastructure/queue/test_task_spec_f3_fields.py -v 2>&1 | tail -15
```

Expected: `AttributeError` — new fields not yet in `TaskSpec`.

- [ ] **Step 3: Add fields to TaskSpec**

In `openbot/infrastructure/queue/task_spec.py`, after `initial_labels: list[str]`, add:

```python
    # F3 fields — optional with defaults (backward-compat with old v3 JSON)
    classifier_output: dict[str, Any] | None = None
    is_incremental: bool = False
    is_force_push: bool = False
```

Update `from_event_and_dispatch` signature to accept the new kwargs and set `classifier_skipped`:

```python
    @classmethod
    def from_event_and_dispatch(
        cls,
        event: UnifiedEvent,
        dispatch: Dispatch,
        *,
        check_run_id: int | None = None,
        decision_trace: list[dict[str, Any]] | None = None,
        initial_labels: list[str] | None = None,
        classifier_output: dict[str, Any] | None = None,
        is_incremental: bool = False,
        is_force_push: bool = False,
    ) -> TaskSpec:
        now = datetime.now(UTC).isoformat()
        return cls(
            spec_version=TASK_SPEC_VERSION,
            task_id=dispatch.task_id,
            run_id=dispatch.run_id or dispatch.task_id,
            prev_run_id=dispatch.prev_run_id,
            resource_key=dispatch.resource_key,
            event_seq=dispatch.event_seq,
            intent=dispatch.intent or "start",
            enqueued_at=now,
            spec_built_at=now,
            scenario=dispatch.feature.value,
            channel=event.channel,
            delivery_id=event.delivery_id,
            kind=event.kind.value,
            repo=event.repo,
            actor=event.actor,
            actor_type=event.actor_type,
            issue_number=event.issue_number,
            pr_number=event.pr_number,
            comment_body=event.comment_body,
            installation_id=event.installation_id,
            raw=event.raw,
            check_run_id=check_run_id,
            decision_trace=decision_trace or [],
            classifier_skipped=(classifier_output is None),
            stages_to_run=[],
            initial_labels=initial_labels or [],
            classifier_output=classifier_output,
            is_incremental=is_incremental,
            is_force_push=is_force_push,
        )
```

- [ ] **Step 4: Run tests to confirm PASS**

```bash
python -m pytest tests/infrastructure/queue/test_task_spec_f3_fields.py -v 2>&1 | tail -15
```

Expected: all 5 tests pass.

- [ ] **Step 5: Confirm existing queue tests still pass**

```bash
python -m pytest tests/infrastructure/queue/ -v 2>&1 | tail -15
```

- [ ] **Step 6: Commit**

```bash
git add openbot/infrastructure/queue/task_spec.py tests/infrastructure/queue/test_task_spec_f3_fields.py
git commit -m "feat(task-spec): add classifier_output, is_incremental, is_force_push (F3)"
```

---

### Task 2: Create `openbot/dispatcher/incremental.py`

**Files:**
- Create: `openbot/dispatcher/incremental.py`
- Test: `tests/application/dispatcher/test_incremental.py` (new)

Pure function — zero I/O. Compares `before` SHA in webhook payload with `last_reviewed_sha` to decide full vs incremental diff.

- [ ] **Step 1: Write failing tests**

Create `tests/application/dispatcher/test_incremental.py`:

```python
# tests/application/dispatcher/test_incremental.py
"""Unit tests: compute_diff_scope — pure incremental-review scope logic."""
from __future__ import annotations

import pytest

from openbot.dispatcher.incremental import DiffScope, compute_diff_scope


def _pr_raw(*, head_sha: str, base_sha: str, before: str | None = None) -> dict:
    p: dict = {"pull_request": {"head": {"sha": head_sha}, "base": {"sha": base_sha}}}
    if before is not None:
        p["before"] = before
    return p


def test_first_review_no_last_sha() -> None:
    scope = compute_diff_scope(_pr_raw(head_sha="H1", base_sha="B1"), last_reviewed_sha=None)
    assert scope.head_sha == "H1"
    assert scope.base_sha == "B1"
    assert scope.is_incremental is False
    assert scope.is_force_push is False
    assert scope.last_reviewed_sha is None


def test_incremental_normal_push() -> None:
    """before_sha == last_reviewed_sha → incremental diff from last_reviewed to head."""
    scope = compute_diff_scope(
        _pr_raw(head_sha="H2", base_sha="B1", before="PREV1"),
        last_reviewed_sha="PREV1",
    )
    assert scope.base_sha == "PREV1"   # diff from last review point
    assert scope.head_sha == "H2"
    assert scope.is_incremental is True
    assert scope.is_force_push is False


def test_force_push_before_differs() -> None:
    """before_sha ≠ last_reviewed_sha → force push, full diff from PR base."""
    scope = compute_diff_scope(
        _pr_raw(head_sha="H3", base_sha="B1", before="OTHER"),
        last_reviewed_sha="PREV1",
    )
    assert scope.base_sha == "B1"
    assert scope.is_incremental is False
    assert scope.is_force_push is True


def test_missing_before_with_last_reviewed_is_force_push() -> None:
    """No 'before' key + last_reviewed_sha present → conservative force push."""
    scope = compute_diff_scope(_pr_raw(head_sha="H4", base_sha="B1"), last_reviewed_sha="PREV1")
    assert scope.is_force_push is True
    assert scope.is_incremental is False


def test_non_pr_payload_returns_empty_scope() -> None:
    scope = compute_diff_scope({}, last_reviewed_sha=None)
    assert scope.head_sha is None
    assert scope.base_sha is None
    assert scope.is_incremental is False
    assert scope.is_force_push is False


def test_diff_scope_is_frozen() -> None:
    scope = compute_diff_scope({}, last_reviewed_sha=None)
    with pytest.raises(Exception):
        scope.head_sha = "mutated"  # type: ignore[misc]
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
python -m pytest tests/application/dispatcher/test_incremental.py -v 2>&1 | tail -15
```

Expected: `ModuleNotFoundError: openbot.dispatcher.incremental`.

- [ ] **Step 3: Create `openbot/dispatcher/incremental.py`**

```python
# openbot/dispatcher/incremental.py
"""Incremental PR review scope computation (pure, no I/O).

Determines whether a PR synchronize event can be reviewed incrementally
(last_reviewed_sha → head_sha) or needs a full re-review from the PR base.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiffScope:
    """Resolved diff boundary for a PR review task.

    Attributes:
        base_sha: SHA to diff FROM (last_reviewed_sha for incremental; PR base for full).
        head_sha: SHA to diff TO (always the current PR head commit).
        is_incremental: True when only new commits since last review need checking.
        is_force_push: True when history was rewritten; full re-review required.
        last_reviewed_sha: The end-SHA from the previous review run, or None.
    """

    base_sha: str | None
    head_sha: str | None
    is_incremental: bool
    is_force_push: bool
    last_reviewed_sha: str | None


def compute_diff_scope(
    raw: dict[str, object],
    *,
    last_reviewed_sha: str | None,
) -> DiffScope:
    """Compute diff boundary from a raw PR webhook payload.

    Args:
        raw: ``event.raw`` dict from a PR opened/synchronize event.
        last_reviewed_sha: Commit SHA where the previous review run ended,
            or None for the first review.

    Returns:
        DiffScope with resolved boundaries and incremental/force-push flags.
    """
    pull_request = raw.get("pull_request") if isinstance(raw, dict) else None
    if not isinstance(pull_request, dict):
        return DiffScope(
            base_sha=None,
            head_sha=None,
            is_incremental=False,
            is_force_push=False,
            last_reviewed_sha=last_reviewed_sha,
        )

    head = pull_request.get("head")
    base = pull_request.get("base")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    base_sha = base.get("sha") if isinstance(base, dict) else None

    if last_reviewed_sha is None:
        return DiffScope(
            base_sha=base_sha,
            head_sha=head_sha,
            is_incremental=False,
            is_force_push=False,
            last_reviewed_sha=None,
        )

    before_sha = raw.get("before") if isinstance(raw, dict) else None

    if before_sha != last_reviewed_sha:
        # 'before' absent or mismatched → force push; history may be rewritten.
        return DiffScope(
            base_sha=base_sha,
            head_sha=head_sha,
            is_incremental=False,
            is_force_push=True,
            last_reviewed_sha=last_reviewed_sha,
        )

    # Normal incremental push: diff from last review end to new head.
    return DiffScope(
        base_sha=last_reviewed_sha,
        head_sha=head_sha,
        is_incremental=True,
        is_force_push=False,
        last_reviewed_sha=last_reviewed_sha,
    )
```

- [ ] **Step 4: Run tests to confirm PASS**

```bash
python -m pytest tests/application/dispatcher/test_incremental.py -v 2>&1 | tail -15
```

Expected: all 6 tests pass.

- [ ] **Step 5: Run full dispatcher suite**

```bash
python -m pytest tests/application/dispatcher/ -v 2>&1 | tail -15
```

- [ ] **Step 6: Commit**

```bash
git add openbot/dispatcher/incremental.py tests/application/dispatcher/test_incremental.py
git commit -m "feat(dispatcher): add incremental.py — pure DiffScope computation (F3)"
```

---

**Continue with Part 2** (`2026-05-20-webhook-worker-layering-f3-part2.md`) for Task 3 (classifier.py).
**Continue with Part 3** (`2026-05-20-webhook-worker-layering-f3-part3.md`) for Tasks 4–5 (wiring + acceptance).
