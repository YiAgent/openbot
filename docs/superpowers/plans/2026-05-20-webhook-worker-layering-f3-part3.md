# Webhook-Worker Layering F3 — Plan (Part 3: Tasks 4–5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Context:** Parts 1–2 delivered TaskSpec F3 fields, `incremental.py`, and `classifier.py`. This part wires D10 into `decide_and_enqueue()` and runs the final acceptance verification.

**Key design:**
- In `decide_and_enqueue()`, after D12 direct-action check, call `classify_event()` → convert result with `dataclasses.asdict()` → pass as `classifier_output` to `TaskSpec.from_event_and_dispatch()`.
- For PR events only, call `compute_diff_scope()` with `last_reviewed_sha=None` (v0.1: DB lookup deferred).
- `stages_to_run` is populated inside `from_event_and_dispatch` **after** this wiring is complete — actually, `stages_to_run` is hardcoded `[]` in `from_event_and_dispatch`; caller must set it. See Step 3 below for the exact updated call site.

---

### Task 4: Wire D10 + incremental into `decide_and_enqueue()`

**Files:**
- Modify: `openbot/dispatcher/decide.py`
- Test: `tests/application/dispatcher/test_decide_f3.py` (new)

After the D12 direct-action block (and before the `if queue is not None` block), insert D13:
1. `classify_event()` → typed output → `asdict()` → stored as `classifier_output` dict
2. `compute_diff_scope()` for PR events → `is_incremental`, `is_force_push`
3. `stages_from_classifier()` → pass as `stages_to_run` to `TaskSpec.from_event_and_dispatch()`

- [ ] **Step 1: Write failing tests**

Create `tests/application/dispatcher/test_decide_f3.py`:

```python
# tests/application/dispatcher/test_decide_f3.py
"""Integration tests: decide_and_enqueue() D10 wiring (F3)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openbot.dispatcher import decide_and_enqueue
from openbot.domain.events import EventKind, UnifiedEvent
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests._fakes.queue import FakeQueue


def _issue_event(body: str) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github", delivery_id="del-f3-issue",
        kind=EventKind.ISSUE_OPENED, repo="org/repo", actor="alice",
        actor_type="User", issue_number=9, pr_number=None,
        installation_id=100, comment_body=None,
        raw={"issue": {"number": 9, "body": body, "labels": []}},
    )


def _pr_event(head_sha: str = "SHA-head", before: str | None = None) -> UnifiedEvent:
    raw: dict = {
        "pull_request": {
            "number": 42,
            "additions": 10, "deletions": 5, "changed_files": 2,
            "head": {"sha": head_sha}, "base": {"sha": "SHA-base"},
        }
    }
    if before is not None:
        raw["before"] = before
    return UnifiedEvent(
        channel="github", delivery_id="del-f3-pr",
        kind=EventKind.PR_OPENED, repo="org/repo", actor="alice",
        actor_type="User", issue_number=None, pr_number=42,
        installation_id=100, comment_body=None, raw=raw,
    )


@pytest.mark.asyncio
async def test_classifier_output_stored_in_task_spec() -> None:
    """Classifier result is serialised into TaskSpec.classifier_output."""
    from openbot.application.router import dispatch_for

    event = _issue_event("This is a bug: app crashes when clicking submit.")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    classifier_data = {
        "type": "bug", "severity_guess": "high",
        "has_reproduction_info": True, "looks_like_spam": False,
    }
    response = MagicMock()
    response.choices[0].message.content = json.dumps(classifier_data)

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event, dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue, session_factory=None, redis=None,
        )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.classifier_output == classifier_data
    assert spec.classifier_skipped is False


@pytest.mark.asyncio
async def test_classifier_failure_is_fail_open() -> None:
    """LLM exception → classifier_skipped=True, spec still enqueued."""
    from openbot.application.router import dispatch_for

    event = _issue_event("App crashes on login.")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=RuntimeError("down")):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event, dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue, session_factory=None, redis=None,
        )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.classifier_output is None
    assert spec.classifier_skipped is True


@pytest.mark.asyncio
async def test_stages_to_run_populated_from_classifier() -> None:
    """stages_to_run reflects classifier output (bug with repro → includes 'reproduce')."""
    from openbot.application.router import dispatch_for

    event = _issue_event("Bug: crash with full stack trace attached.")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    response = MagicMock()
    response.choices[0].message.content = json.dumps({
        "type": "bug", "severity_guess": "high",
        "has_reproduction_info": True, "looks_like_spam": False,
    })

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event, dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue, session_factory=None, redis=None,
        )

    spec = queue.task_specs[0]
    assert "reproduce" in spec.stages_to_run
    assert "classify_labels" in spec.stages_to_run


@pytest.mark.asyncio
async def test_pr_event_gets_incremental_fields() -> None:
    """PR event → DiffScope computed, is_incremental/is_force_push stored in spec."""
    from openbot.application.router import dispatch_for

    event = _pr_event(head_sha="SHA-new", before="SHA-old")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    response = MagicMock()
    response.choices[0].message.content = json.dumps({
        "change_size_class": "s", "touches_security_paths": False,
        "is_breaking": False, "suggested_subagents": ["correctness"],
    })

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event, dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue, session_factory=None, redis=None,
        )

    # With last_reviewed_sha=None (v0.1 default) → first review, not incremental
    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.is_force_push is False


@pytest.mark.asyncio
async def test_decide_and_enqueue_still_never_raises() -> None:
    """decide_and_enqueue must swallow all exceptions including classifier errors."""
    from openbot.application.router import dispatch_for

    event = _issue_event("Something.")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    # Intentionally cause an unexpected error inside classify_event path
    with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=Exception("boom")):
        # Should not raise
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event, dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=FakeQueue(), session_factory=None, redis=None,
        )
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
cd /Users/wy/projects/openbot/.worktrees/feat-webhook-worker-layering-f2
python -m pytest tests/application/dispatcher/test_decide_f3.py -v 2>&1 | tail -20
```

Expected: tests checking `classifier_output` fail — field present but empty (not yet wired).

- [ ] **Step 3: Wire D10 into `decide_and_enqueue()`**

Read `openbot/dispatcher/decide.py`. The top-level imports already include `extract_event_context`, `check_issue_completeness`, etc. Add these imports:

```python
from dataclasses import asdict as _dataclass_asdict

from openbot.dispatcher.classifier import classify_event, stages_from_classifier
from openbot.dispatcher.incremental import compute_diff_scope
```

Then find the block that builds `initial_labels` and enqueues. Replace:

```python
        initial_labels = _extract_initial_labels(event.raw)

        if queue is not None:
            spec = TaskSpec.from_event_and_dispatch(
                event,
                dispatch,
                check_run_id=check_run_id,
                decision_trace=[],
                initial_labels=initial_labels,
            )
            await queue.enqueue_task_spec(spec)
```

With:

```python
        initial_labels = _extract_initial_labels(event.raw)

        # D13: Classify event (D10) and compute incremental scope for PRs.
        classifier_result = await classify_event(
            feature=dispatch.feature,
            body=(
                event.comment_body
                or (event.raw.get("issue", {}) or {}).get("body")   # type: ignore[union-attr]
                or (event.raw.get("pull_request", {}) or {}).get("body")  # type: ignore[union-attr]
                or ""
            ),
            redis=redis,
        )
        classifier_output = (
            _dataclass_asdict(classifier_result) if classifier_result is not None else None
        )
        stages = stages_from_classifier(dispatch.feature, classifier_result)

        diff_scope = compute_diff_scope(event.raw, last_reviewed_sha=None)

        if queue is not None:
            spec = TaskSpec.from_event_and_dispatch(
                event,
                dispatch,
                check_run_id=check_run_id,
                decision_trace=[],
                initial_labels=initial_labels,
                classifier_output=classifier_output,
                is_incremental=diff_scope.is_incremental,
                is_force_push=diff_scope.is_force_push,
            )
            # Patch stages_to_run after construction (frozen dataclass requires replace)
            import dataclasses
            spec = dataclasses.replace(spec, stages_to_run=stages)
            await queue.enqueue_task_spec(spec)
```

**Note:** `dataclasses.replace()` on a frozen dataclass returns a new instance with the specified fields changed — it does NOT mutate the original.

Also update the log call to include stages:

```python
            _logger.info(
                "decide_and_enqueue_queued",
                extra={
                    "delivery_id": event.delivery_id,
                    "repo": event.repo,
                    "scenario": spec.scenario,
                    "task_id": spec.task_id,
                    "classifier_skipped": spec.classifier_skipped,
                    "stages_to_run": spec.stages_to_run,
                    "is_incremental": spec.is_incremental,
                },
            )
```

- [ ] **Step 4: Run F3 integration tests to confirm PASS**

```bash
python -m pytest tests/application/dispatcher/test_decide_f3.py -v 2>&1 | tail -20
```

Expected: all 5 tests pass.

- [ ] **Step 5: Confirm all dispatcher tests still pass**

```bash
python -m pytest tests/application/dispatcher/ -v 2>&1 | tail -20
```

- [ ] **Step 6: Commit**

```bash
git add openbot/dispatcher/decide.py tests/application/dispatcher/test_decide_f3.py
git commit -m "feat(dispatcher): wire D10 LLM classifier + incremental scope into decide_and_enqueue (F3)"
```

---

### Task 5: Final Check — `make check` and F3 Acceptance Verification

**Files:**
- No new files

- [ ] **Step 1: Run `make check`**

```bash
cd /Users/wy/projects/openbot/.worktrees/feat-webhook-worker-layering-f2
make check 2>&1 | tail -30
```

Expected: `fmt-check` passes, `lint` passes, all tests pass. If `fmt-check` fails, run `make fmt` first then re-run `make check`.

- [ ] **Step 2: Verify F3 acceptance criteria (spec §8)**

Run the F3 test suites:

```bash
python -m pytest \
  tests/infrastructure/queue/test_task_spec_f3_fields.py \
  tests/application/dispatcher/test_incremental.py \
  tests/application/dispatcher/test_classifier.py \
  tests/application/dispatcher/test_decide_f3.py \
  -v 2>&1
```

Expected (all PASS):
```
test_task_spec_f3_fields.py::test_new_fields_default_to_none_false PASSED
test_task_spec_f3_fields.py::test_new_fields_roundtrip PASSED
test_task_spec_f3_fields.py::test_old_v3_json_without_new_fields PASSED
test_task_spec_f3_fields.py::test_from_event_and_dispatch_defaults PASSED
test_task_spec_f3_fields.py::test_from_event_and_dispatch_with_classifier_output PASSED
test_incremental.py::test_first_review_no_last_sha PASSED
test_incremental.py::test_incremental_normal_push PASSED
test_incremental.py::test_force_push_before_differs PASSED
test_incremental.py::test_missing_before_with_last_reviewed_is_force_push PASSED
test_incremental.py::test_non_pr_payload_returns_empty_scope PASSED
test_incremental.py::test_diff_scope_is_frozen PASSED
test_classifier.py::test_triage_bug_with_repro_includes_reproduce_stage PASSED
test_classifier.py::test_triage_feature_no_reproduce_stage PASSED
test_classifier.py::test_review_uses_suggested_subagents PASSED
test_classifier.py::test_review_empty_suggested_falls_back_to_correctness PASSED
test_classifier.py::test_chat_readonly_qa PASSED
test_classifier.py::test_chat_unclear_returns_empty_list PASSED
test_classifier.py::test_none_output_returns_empty_list PASSED
test_classifier.py::test_fix_with_none_returns_empty PASSED
test_classifier.py::test_classify_triage_success PASSED
test_classifier.py::test_classify_llm_exception_returns_none PASSED
test_classifier.py::test_classify_invalid_json_returns_none PASSED
test_classifier.py::test_classify_review_happy_path PASSED
test_classifier.py::test_classify_redis_cache_hit_skips_llm PASSED
test_classifier.py::test_classify_redis_miss_calls_llm_and_stores PASSED
test_classifier.py::test_classify_fix_returns_none PASSED
test_decide_f3.py::test_classifier_output_stored_in_task_spec PASSED
test_decide_f3.py::test_classifier_failure_is_fail_open PASSED
test_decide_f3.py::test_stages_to_run_populated_from_classifier PASSED
test_decide_f3.py::test_pr_event_gets_incremental_fields PASSED
test_decide_f3.py::test_decide_and_enqueue_still_never_raises PASSED
```

- [ ] **Step 3: Count total tests**

```bash
python -m pytest --co -q 2>&1 | tail -5
```

Expected: ≥ 760 tests (730 F1 baseline + ~11 F2 new + ~31 F3 new).

- [ ] **Step 4: Commit final state**

```bash
git add -A
git commit -m "chore: final F3 verification — make check green, 760+ tests passing"
```

---

**F3 complete.** Use `superpowers:finishing-a-development-branch` to push and create the PR.

## F3 Feature Summary

| Component | File | Purpose |
|-----------|------|---------|
| TaskSpec extension | `openbot/infrastructure/queue/task_spec.py` | `classifier_output`, `is_incremental`, `is_force_push` fields + updated `from_event_and_dispatch` |
| Incremental scope | `openbot/dispatcher/incremental.py` | Pure `DiffScope` + `compute_diff_scope()` |
| LLM classifier | `openbot/dispatcher/classifier.py` | D10 — one-shot litellm + Redis cache; typed output dataclasses |
| Wiring | `openbot/dispatcher/decide.py` | D13 block: classify → stages_to_run → spec with F3 fields |
