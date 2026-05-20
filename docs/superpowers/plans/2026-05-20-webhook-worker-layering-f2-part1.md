# Webhook-Worker Layering F2 — Implementation Plan (Part 1: Tasks 1–4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement F2 direct-action short-circuit (D11+D12) so the webhook async segment can reply with a canned message and return without enqueuing when it detects an empty issue body, an oversized PR, or a vague @mention.

**Architecture:** Three layers — (1) pure context extraction (`dispatcher/context.py`), (2) pure rule evaluation (`dispatcher/direct_actions.py`), (3) wiring into `decide_and_enqueue()` after D2-D9 preflight. Also fixes classifier UNLABELED branches (§6.1 必做) and extends `ChannelAdapterPort` with `add_label` so the needs-info label can be applied.

**Tech Stack:** Python 3.12, pytest-asyncio, fakeredis, ruff/mypy for checks.

---

### Task 1: Fix UNLABELED Classifier Branches

**Files:**
- Modify: `openbot/application/state/classifier.py`
- Test: `tests/application/state/test_classifier.py`

The spec §6.1 marks this as 必做 (required). `ISSUE_UNLABELED` and `PR_UNLABELED` currently fall through to the `"unhandled_kind"` catch-all instead of returning `Intent.IGNORE` with reason `"label_removed"`.

- [ ] **Step 1: Write the failing tests**

Open `tests/application/state/test_classifier.py` and add at the bottom:

```python
@pytest.mark.parametrize("kind", [EventKind.ISSUE_UNLABELED, EventKind.PR_UNLABELED])
def test_unlabeled_events_return_ignore(kind: EventKind) -> None:
    """UNLABELED events must return IGNORE / label_removed, not unhandled_kind."""
    from openbot.application.state.classifier import classify
    from openbot.domain.events import UnifiedEvent
    from openbot.infrastructure.persistence.models import IssueState

    event = UnifiedEvent(
        channel="github",
        delivery_id="del-1",
        kind=kind,
        repo="org/r",
        actor="alice",
        actor_type="User",
        issue_number=7,
        pr_number=None if kind is EventKind.ISSUE_UNLABELED else 7,
        installation_id=100,
        comment_body=None,
        raw={},
    )
    result = classify(event, IssueState.OPEN, frozenset())
    assert result.intent is Intent.IGNORE
    assert result.reason == "label_removed"
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
cd /Users/wy/projects/openbot/.worktrees/feat-webhook-worker-layering-f2
python -m pytest tests/application/state/test_classifier.py::test_unlabeled_events_return_ignore -v 2>&1 | tail -20
```

Expected: `FAILED` — the current classifier returns `"unhandled_kind"` not `"label_removed"`.

- [ ] **Step 3: Read classifier then add UNLABELED block**

Read `openbot/application/state/classifier.py` to find the LABELED handling block (around line 113), then add before it:

```python
    if kind in (EventKind.ISSUE_UNLABELED, EventKind.PR_UNLABELED):
        return EventClassification(
            intent=Intent.IGNORE,
            next_state=current_state,
            reason="label_removed",
        )
```

The block must appear **before** the LABELED block so both unlabeled and labeled events are handled by their own explicit branches.

- [ ] **Step 4: Run tests to confirm PASS**

```bash
python -m pytest tests/application/state/test_classifier.py -v 2>&1 | tail -20
```

Expected: all classifier tests pass, including the new parametrize cases.

- [ ] **Step 5: Run full suite to check for regressions**

```bash
python -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: same number of passing tests as before ± 2 (the 2 new ones).

- [ ] **Step 6: Commit**

```bash
git add openbot/application/state/classifier.py tests/application/state/test_classifier.py
git commit -m "fix(classifier): explicit IGNORE branch for ISSUE/PR_UNLABELED events"
```

---

### Task 2: Add `add_label` to ChannelAdapterPort and FakeChannelAdapter

**Files:**
- Modify: `openbot/application/ports/channel_adapter.py`
- Modify: `tests/_fakes/channel_adapter.py`
- Test: `tests/application/ports/test_channel_adapter_protocol.py` (new, small)

`GitHubAdapter` already has `add_label`. It's not on the `ChannelAdapterPort` Protocol, so direct-action code can't call it through the port. `FakeChannelAdapter` also needs the method for test doubles.

- [ ] **Step 1: Write the failing protocol conformance test**

Create `tests/application/ports/test_channel_adapter_protocol.py`:

```python
"""Verify FakeChannelAdapter satisfies the full ChannelAdapterPort protocol."""
from __future__ import annotations

import pytest

from openbot.application.ports.channel_adapter import ChannelAdapterPort
from tests._fakes.channel_adapter import FakeChannelAdapter


def test_fake_channel_adapter_satisfies_protocol() -> None:
    """Runtime isinstance check against the Protocol."""
    adapter: ChannelAdapterPort = FakeChannelAdapter()  # type: ignore[assignment]
    # Protocol is runtime_checkable — will raise if any method is missing
    assert isinstance(adapter, ChannelAdapterPort)
```

- [ ] **Step 2: Run to confirm FAIL (or pass if already runtime_checkable)**

```bash
python -m pytest tests/application/ports/test_channel_adapter_protocol.py -v 2>&1 | tail -20
```

This may pass if the protocol doesn't yet declare `add_label` (because `FakeChannelAdapter` won't be missing it). That's fine — the structural test below catches it.

- [ ] **Step 3: Add `add_label` to `ChannelAdapterPort`**

Read `openbot/application/ports/channel_adapter.py`, then append after `fetch_repo_file`:

```python
    async def add_label(self, event: UnifiedEvent, *labels: str) -> list[dict[str, Any]]:
        """Add one or more labels to the issue or PR referenced by *event*.

        Returns a list of created label objects (same shape as GitHub API response).
        Implementations may return [] on a no-op or if labels already exist.
        """
        ...
```

Also ensure the Protocol class is decorated with `@runtime_checkable`:

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class ChannelAdapterPort(Protocol):
    ...
```

- [ ] **Step 4: Add `add_label` + `labels_added` to `FakeChannelAdapter`**

Read `tests/_fakes/channel_adapter.py`, then add the tracking field and method:

```python
    # Add to the @dataclass fields:
    labels_added: list[tuple[str | None, list[str]]] = field(default_factory=list)

    # Add as a new async method:
    async def add_label(self, event: UnifiedEvent, *labels: str) -> list[dict[str, Any]]:
        self.labels_added.append((event.resource_key, list(labels)))
        return [{"name": lbl} for lbl in labels]
```

- [ ] **Step 5: Run the protocol test + full suite**

```bash
python -m pytest tests/application/ports/test_channel_adapter_protocol.py tests/_fakes/ -v 2>&1 | tail -20
python -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add openbot/application/ports/channel_adapter.py tests/_fakes/channel_adapter.py tests/application/ports/test_channel_adapter_protocol.py
git commit -m "feat(ports): add add_label to ChannelAdapterPort + FakeChannelAdapter"
```

---

### Task 3: Create `dispatcher/context.py` — Pure Event Context Extraction

**Files:**
- Create: `openbot/dispatcher/context.py`
- Create: `tests/dispatcher/test_context.py`

This is a pure-function module: no I/O, no async. It extracts structured fields from `event.raw` so downstream rule code can work with typed data instead of raw dicts.

- [ ] **Step 1: Write the tests first**

Create `tests/dispatcher/test_context.py`:

```python
"""Tests for dispatcher/context.py — pure EventContext extraction."""
from __future__ import annotations

import pytest

from openbot.dispatcher.context import EventContext, extract_event_context
from openbot.domain.events import EventKind, UnifiedEvent


def _event(raw: dict, comment_body: str | None = None) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="del-1",
        kind=EventKind.ISSUE_OPENED,
        repo="org/r",
        actor="alice",
        actor_type="User",
        issue_number=7,
        pr_number=None,
        installation_id=100,
        comment_body=comment_body,
        raw=raw,
    )


def test_empty_body_is_empty_string() -> None:
    """issue.body = "" (explicit empty) → issue_body == "" (not None)."""
    ev = _event({"issue": {"number": 7, "body": ""}})
    ctx = extract_event_context(ev)
    assert ctx.issue_body == ""


def test_null_body_is_none() -> None:
    """issue.body = null in JSON → issue_body is None."""
    ev = _event({"issue": {"number": 7, "body": None}})
    ctx = extract_event_context(ev)
    assert ctx.issue_body is None


def test_absent_body_key_is_none() -> None:
    """No 'body' key in issue dict → issue_body is None."""
    ev = _event({"issue": {"number": 7}})
    ctx = extract_event_context(ev)
    assert ctx.issue_body is None


def test_whitespace_only_body_is_not_empty() -> None:
    """Whitespace-only body is non-empty (callers strip if they want)."""
    ev = _event({"issue": {"number": 7, "body": "   "}})
    ctx = extract_event_context(ev)
    assert ctx.issue_body == "   "


def test_issue_labels_extracted() -> None:
    raw = {"issue": {"labels": [{"name": "bug"}, {"name": "triage"}]}}
    ctx = extract_event_context(_event(raw))
    assert ctx.issue_labels == ("bug", "triage")


def test_pr_stats_extracted() -> None:
    raw = {"pull_request": {"additions": 300, "deletions": 250, "changed_files": 12}}
    ctx = extract_event_context(_event(raw))
    assert ctx.pr_additions == 300
    assert ctx.pr_deletions == 250
    assert ctx.pr_changed_files == 12
    assert ctx.pr_total_lines_changed == 550


def test_empty_raw_gives_defaults() -> None:
    ctx = extract_event_context(_event({}))
    assert ctx.issue_body is None
    assert ctx.issue_labels == ()
    assert ctx.pr_total_lines_changed == 0


def test_mention_body_from_comment() -> None:
    ctx = extract_event_context(_event({}, comment_body="@openbot help"))
    assert ctx.mention_body == "@openbot help"


def test_mention_body_none_when_no_comment() -> None:
    ctx = extract_event_context(_event({}))
    assert ctx.mention_body is None
```

- [ ] **Step 2: Run to confirm FAIL (module not yet created)**

```bash
python -m pytest tests/dispatcher/test_context.py -v 2>&1 | tail -20
```

Expected: `ModuleNotFoundError` or `ImportError` for `openbot.dispatcher.context`.

- [ ] **Step 3: Create `openbot/dispatcher/context.py`**

```python
# openbot/dispatcher/context.py
"""D11: Pure event-context extraction for direct-action rule evaluation.

Converts ``UnifiedEvent.raw`` (untyped dict) into a typed ``EventContext``
so downstream rule functions work with structured data and no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openbot.domain.events import UnifiedEvent

__all__ = ["EventContext", "extract_event_context"]


@dataclass(frozen=True, slots=True)
class EventContext:
    """Structured fields extracted from a GitHub webhook payload.

    ``issue_body``:
      - ``None``  — the "body" key was absent from the payload (e.g. label events)
      - ``""``    — the body key was present but explicitly empty or null in JSON
      - otherwise — the actual body string

    ``pr_additions``, ``pr_deletions``, ``pr_changed_files``:
      Zero when not a PR event or when the payload omits these fields.
    """

    issue_body: str | None
    issue_title: str | None
    issue_labels: tuple[str, ...]
    pr_additions: int
    pr_deletions: int
    pr_changed_files: int
    mention_body: str | None

    @property
    def pr_total_lines_changed(self) -> int:
        """Sum of additions + deletions (lines touched, not net diff)."""
        return self.pr_additions + self.pr_deletions


def extract_event_context(event: UnifiedEvent) -> EventContext:
    """Extract structured context from *event* without performing any I/O."""
    raw: dict[str, Any] = event.raw or {}
    issue: dict[str, Any] = raw.get("issue") or {}
    pr: dict[str, Any] = raw.get("pull_request") or {}

    # Distinguish absent key from JSON null / empty string.
    if "body" not in issue:
        issue_body: str | None = None
    else:
        raw_body = issue["body"]
        issue_body = str(raw_body) if raw_body is not None else None

    issue_labels: list[str] = []
    for lbl in issue.get("labels") or []:
        if isinstance(lbl, dict) and lbl.get("name"):
            issue_labels.append(str(lbl["name"]))

    return EventContext(
        issue_body=issue_body,
        issue_title=issue.get("title"),
        issue_labels=tuple(issue_labels),
        pr_additions=int(pr.get("additions") or 0),
        pr_deletions=int(pr.get("deletions") or 0),
        pr_changed_files=int(pr.get("changed_files") or 0),
        mention_body=event.comment_body,
    )
```

- [ ] **Step 4: Ensure `tests/dispatcher/__init__.py` exists**

```bash
touch tests/dispatcher/__init__.py
```

- [ ] **Step 5: Run context tests to confirm PASS**

```bash
python -m pytest tests/dispatcher/test_context.py -v 2>&1 | tail -20
```

Expected: 9 tests pass.

- [ ] **Step 6: Run full suite**

```bash
python -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add openbot/dispatcher/context.py tests/dispatcher/__init__.py tests/dispatcher/test_context.py
git commit -m "feat(dispatcher): EventContext + extract_event_context (pure, D11)"
```

---

### Task 4: Create `dispatcher/direct_actions.py` — Pure Rule Functions

**Files:**
- Create: `openbot/dispatcher/direct_actions.py`
- Create: `tests/dispatcher/test_direct_actions.py`

Three pure functions evaluate the extracted context and return a `DirectAction` (message + optional labels + drop=True) or `None` to indicate "no direct action, continue to enqueue."

- [ ] **Step 1: Write the tests first**

Create `tests/dispatcher/test_direct_actions.py`:

```python
"""Tests for dispatcher/direct_actions.py — pure rule evaluation."""
from __future__ import annotations

import pytest

from openbot.dispatcher.context import EventContext
from openbot.dispatcher.direct_actions import (
    PR_OVERSIZED_THRESHOLD,
    DirectAction,
    check_issue_completeness,
    check_mention_clarity,
    check_pr_size,
)


def _ctx(
    *,
    issue_body: str | None = None,
    issue_title: str | None = None,
    issue_labels: tuple[str, ...] = (),
    pr_additions: int = 0,
    pr_deletions: int = 0,
    pr_changed_files: int = 0,
    mention_body: str | None = None,
) -> EventContext:
    return EventContext(
        issue_body=issue_body,
        issue_title=issue_title,
        issue_labels=issue_labels,
        pr_additions=pr_additions,
        pr_deletions=pr_deletions,
        pr_changed_files=pr_changed_files,
        mention_body=mention_body,
    )


# ─── check_issue_completeness ─────────────────────────────────────────────────

class TestCheckIssueCompleteness:
    def test_absent_body_key_no_action(self) -> None:
        """body=None (absent key) → no action (can't distinguish from normal)."""
        assert check_issue_completeness(_ctx(issue_body=None)) is None

    def test_body_with_content_no_action(self) -> None:
        assert check_issue_completeness(_ctx(issue_body="Describe the bug here")) is None

    def test_whitespace_only_body_triggers_action(self) -> None:
        action = check_issue_completeness(_ctx(issue_body="   \n  "))
        assert action is not None
        assert "needs-info" in action.labels_to_add
        assert action.drop is True

    def test_empty_string_body_triggers_action(self) -> None:
        action = check_issue_completeness(_ctx(issue_body=""))
        assert action is not None
        assert "needs-info" in action.labels_to_add

    def test_message_is_non_empty(self) -> None:
        action = check_issue_completeness(_ctx(issue_body=""))
        assert action is not None
        assert len(action.message) > 20


# ─── check_pr_size ────────────────────────────────────────────────────────────

class TestCheckPrSize:
    def test_small_pr_no_action(self) -> None:
        ctx = _ctx(pr_additions=100, pr_deletions=50)
        assert check_pr_size(ctx) is None

    def test_exactly_threshold_no_action(self) -> None:
        """Boundary: exactly at threshold → no action (> not >=)."""
        ctx = _ctx(pr_additions=PR_OVERSIZED_THRESHOLD, pr_deletions=0)
        assert check_pr_size(ctx) is None

    def test_one_over_threshold_triggers_action(self) -> None:
        ctx = _ctx(pr_additions=PR_OVERSIZED_THRESHOLD, pr_deletions=1)
        action = check_pr_size(ctx)
        assert action is not None
        assert action.drop is True

    def test_message_mentions_line_count(self) -> None:
        total = PR_OVERSIZED_THRESHOLD + 100
        ctx = _ctx(pr_additions=total, pr_deletions=0)
        action = check_pr_size(ctx)
        assert action is not None
        assert str(total) in action.message

    def test_no_labels_for_oversized_pr(self) -> None:
        ctx = _ctx(pr_additions=600, pr_deletions=0)
        action = check_pr_size(ctx)
        assert action is not None
        assert action.labels_to_add == []


# ─── check_mention_clarity ────────────────────────────────────────────────────

class TestCheckMentionClarity:
    def test_no_mention_no_action(self) -> None:
        assert check_mention_clarity(_ctx(mention_body=None)) is None

    def test_empty_mention_triggers_action(self) -> None:
        action = check_mention_clarity(_ctx(mention_body=""))
        assert action is not None

    def test_whitespace_mention_triggers_action(self) -> None:
        action = check_mention_clarity(_ctx(mention_body="   "))
        assert action is not None

    def test_mention_prefix_only_triggers_action(self) -> None:
        """Just "@openbot " with nothing after → vague."""
        action = check_mention_clarity(_ctx(mention_body="@openbot "))
        assert action is not None

    def test_short_mention_triggers_action(self) -> None:
        """Short body (< _MENTION_MIN_CHARS after prefix strip) → vague."""
        action = check_mention_clarity(_ctx(mention_body="@openbot hi"))
        assert action is not None

    def test_substantive_mention_no_action(self) -> None:
        """Long enough body → no action, let the LLM handle it."""
        long_body = "@openbot can you triage this issue and assign the bug label please?"
        assert check_mention_clarity(_ctx(mention_body=long_body)) is None

    def test_direct_mention_without_prefix(self) -> None:
        """Body without @openbot prefix counted from start."""
        long_body = "This is a detailed request about the authentication system failing."
        assert check_mention_clarity(_ctx(mention_body=long_body)) is None
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
python -m pytest tests/dispatcher/test_direct_actions.py -v 2>&1 | tail -20
```

Expected: `ImportError` — module not yet created.

- [ ] **Step 3: Create `openbot/dispatcher/direct_actions.py`**

```python
# openbot/dispatcher/direct_actions.py
"""D12: Pure rule functions that evaluate EventContext and return a DirectAction.

Each function returns ``DirectAction`` if a canned reply should be sent
immediately (short-circuit, no enqueue) or ``None`` to let normal flow continue.

All functions are synchronous and side-effect-free — they only read the context.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from openbot.dispatcher.context import EventContext

__all__ = [
    "DirectAction",
    "PR_OVERSIZED_THRESHOLD",
    "check_issue_completeness",
    "check_pr_size",
    "check_mention_clarity",
]

# ─── Tuneable constants ────────────────────────────────────────────────────────

PR_OVERSIZED_THRESHOLD: int = 500
"""Total lines changed (additions + deletions) above which a PR is flagged."""

_MENTION_MIN_CHARS: int = 20
"""Minimum characters in the stripped mention body to be considered substantive."""

_CHAT_PREFIXES: tuple[str, ...] = ("@openbot ", "@yibots ")
"""Prefixes stripped from mention bodies before measuring length."""


# ─── Result type ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class DirectAction:
    """A canned reply to send without running the full LLM pipeline.

    ``drop`` is always True for v0.1 (return after reply, no enqueue).
    ``labels_to_add`` is empty unless the rule also wants to add a label
    (e.g. ``needs-info`` for empty issue body).
    """

    message: str
    labels_to_add: list[str] = field(default_factory=list)
    drop: bool = True


# ─── Rule functions ───────────────────────────────────────────────────────────

def check_issue_completeness(ctx: EventContext) -> DirectAction | None:
    """Return a DirectAction when an issue body is empty or whitespace-only.

    Returns ``None`` when:
    - ``ctx.issue_body`` is ``None`` (body key absent — not an issue-open event)
    - the body contains non-whitespace text
    """
    if ctx.issue_body is None:
        return None
    if ctx.issue_body.strip():
        return None
    return DirectAction(
        message=(
            "Thanks for opening this issue! 👋\n\n"
            "It looks like the description is empty. Could you add some details "
            "so we can help you better? For example:\n\n"
            "- What are you trying to do?\n"
            "- What did you expect to happen?\n"
            "- What actually happened?\n\n"
            "I've added the **needs-info** label in the meantime. "
            "Feel free to edit the issue body and I'll re-evaluate."
        ),
        labels_to_add=["needs-info"],
    )


def check_pr_size(ctx: EventContext) -> DirectAction | None:
    """Return a DirectAction when a PR changes more lines than the threshold.

    Returns ``None`` when the PR is within the acceptable size.
    """
    total = ctx.pr_total_lines_changed
    if total <= PR_OVERSIZED_THRESHOLD:
        return None
    return DirectAction(
        message=(
            f"This PR changes **{total} lines** across {ctx.pr_changed_files} file(s), "
            f"which is above our review threshold of {PR_OVERSIZED_THRESHOLD} lines.\n\n"
            "Large PRs are harder to review thoroughly. Consider splitting this into "
            "smaller, focused PRs:\n\n"
            "- One PR per logical change or feature\n"
            "- Separate refactoring commits from behavior changes\n"
            "- Extract preparatory changes into a prerequisite PR\n\n"
            "If splitting is not practical, please add a note explaining why."
        ),
        labels_to_add=[],
    )


def check_mention_clarity(ctx: EventContext) -> DirectAction | None:
    """Return a DirectAction when a @mention is too vague to act on.

    Returns ``None`` when:
    - there is no mention body
    - the body is substantive (>= _MENTION_MIN_CHARS after prefix strip)
    """
    if ctx.mention_body is None:
        return None
    body = ctx.mention_body.strip()
    if not body:
        return DirectAction(
            message=(
                "Hi! I'm OpenBot 👋 — you mentioned me but didn't include a request.\n\n"
                "Try something like:\n"
                "- `@openbot triage this issue`\n"
                "- `@openbot review the changes`\n"
                "- `@openbot help me fix this`"
            ),
        )
    for prefix in _CHAT_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    if len(body) < _MENTION_MIN_CHARS:
        return DirectAction(
            message=(
                "Thanks for reaching out! Your message was a bit short for me to act on.\n\n"
                "Could you describe what you need? For example:\n"
                "- `@openbot triage and label this issue`\n"
                "- `@openbot review my PR for correctness`"
            ),
        )
    return None
```

- [ ] **Step 4: Run direct_actions tests to confirm PASS**

```bash
python -m pytest tests/dispatcher/test_direct_actions.py -v 2>&1 | tail -30
```

Expected: all 15 tests pass.

- [ ] **Step 5: Run full suite**

```bash
python -m pytest --tb=short -q 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add openbot/dispatcher/direct_actions.py tests/dispatcher/test_direct_actions.py
git commit -m "feat(dispatcher): DirectAction + pure rule functions (D12)"
```

---

**Continue with Part 2:** `docs/superpowers/plans/2026-05-20-webhook-worker-layering-f2-part2.md` — Tasks 5-6 wire D11+D12 into `decide_and_enqueue()` and add the F-02 end-to-end acceptance test.
