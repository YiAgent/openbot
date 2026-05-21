# Slice C — Fix workflow end-to-end (part 1: domain + schema bridge)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `maybe_run_fix` ACK stub with a real end-to-end loop:
webhook → sandbox clone → DeepAgent edit + run tests → PR if tests pass,
otherwise tailored comment.

**Architecture:** Per-event sandbox lifetime via a `sandbox_factory` on
`PreflightContext`. Domain stays pure (`FixAttempt`, `FixOutcome` are frozen
dataclasses). Schema bridge isolates pydantic at the infrastructure boundary,
mirroring slice B's `_review_schema.py`. `SandboxPort` grows from 1 method
(`run`) to 8 (clone, read_file, write_file, list_files, run, git_diff,
commit_and_push, close) with a new `ExecResult` frozen dataclass.

**Tech Stack:** Python 3.12, pytest, pydantic v2 (infrastructure only),
DeepAgents (`create_deep_agent`), LangChain `StructuredTool`, `httpx`, Daytona
SDK (mocked in unit tests; real impl lands in part 3, task C.5).

**Source spec:** `docs/superpowers/specs/2026-05-20-fix-deepagent-design.md`.
**Reads with:** part 2 (C.3), part 3 (C.4), part 4 (C.5), part 5 (C.6), part 6 (C.7), part 7 (C.8), part 8 (C.9).

**Branch:** `feat/review-deepagent`. Each commit ends with green `make check`.
No `--no-verify`.

---

## File structure for slice C (full map)

| Path | New / Modify | Owning task |
|---|---|---|
| `openbot/domain/fix.py` | NEW | C.1 |
| `tests/domain/test_fix.py` | NEW | C.1 |
| `openbot/infrastructure/agents/_fix_schema.py` | NEW | C.2 |
| `tests/infrastructure/agents/test_fix_schema.py` | NEW | C.2 |
| `openbot/application/ports/sandbox.py` | MODIFY (1 → 8 methods + `ExecResult`) | C.3 (part 2) |
| `openbot/infrastructure/sandboxes/__init__.py` | NEW | C.3 (part 2) |
| `openbot/infrastructure/sandboxes/fake.py` | NEW | C.3 (part 2) |
| `tests/infrastructure/sandboxes/__init__.py` | NEW | C.3 (part 2) |
| `tests/infrastructure/sandboxes/test_fake.py` | NEW | C.3 (part 2) |
| `openbot/application/ports/channel_adapter.py` | MODIFY (+4 methods) | C.4 (part 3) |
| `openbot/infrastructure/adapters/github.py` | MODIFY (+4 impls) | C.4 (part 3) |
| `tests/infrastructure/adapters/test_github.py` | MODIFY (+8 tests) | C.4 (part 3) |
| `tests/_fakes/channel_adapter.py` | MODIFY (+4 stubs) | C.4 (part 3) |
| `openbot/infrastructure/sandboxes/daytona.py` | NEW | C.5 (part 4) |
| `tests/infrastructure/sandboxes/test_daytona.py` | NEW | C.5 (part 4) |
| `openbot/infrastructure/agents/_fix_tools.py` | NEW | C.6 (part 5) |
| `tests/infrastructure/agents/test_fix_tools.py` | NEW | C.6 (part 5) |
| `openbot/infrastructure/agents/deepagents_fix.py` | NEW | C.7 (part 6) |
| `openbot/infrastructure/agents/__init__.py` | MODIFY (+1 export) | C.7 (part 6) |
| `tests/infrastructure/agents/test_deepagents_fix.py` | NEW | C.7 (part 6) |
| `openbot/application/use_cases/fix.py` | REWRITE | C.8 (part 7) |
| `openbot/application/middleware/preflight.py` | MODIFY (+`sandbox_factory`) | C.8 (part 7) |
| `tests/application/use_cases/test_fix.py` | REWRITE | C.8 (part 7) |
| `tests/e2e/test_spec_demos.py` | MODIFY (+demo 08) | C.9 (part 8) |
| `tests/e2e/conftest.py` | MODIFY (+`pr_creates` recording) | C.9 (part 8) |
| `docs/superpowers/plans/2026-05-20-review-fix-deepagent.md` | MODIFY (status line) | C.9 (part 8) |

---

## Type names locked across the slice

| Name | Defined in | Used by |
|---|---|---|
| `FixAttempt` (frozen dataclass) | `openbot/domain/fix.py` (C.1) | Schema (C.2), responder (C.7), use case (C.8), domain tests (C.1), responder tests (C.7), use-case tests (C.8) |
| `FixOutcome` (frozen dataclass) | `openbot/domain/fix.py` (C.1) | Same as above |
| `_FixAttemptModel` / `FixAttemptSchema` (pydantic) | `openbot/infrastructure/agents/_fix_schema.py` (C.2) | Responder `response_format` (C.7) |
| `_FixOutcomeModel` / `FixOutcomeSchema` (pydantic) | C.2 | Responder `response_format` (C.7) |
| `parse_structured_response(raw) -> FixOutcome` | C.2 | Responder (C.7) |
| `ExecResult` (frozen dataclass) | `openbot/application/ports/sandbox.py` (C.3) | Fake (C.3), Daytona (C.5), tools (C.6) |
| `SandboxPort.clone / read_file / write_file / list_files / run / git_diff / commit_and_push / close` | C.3 | Adapters (C.3, C.5), tools (C.6), use case (C.8) |
| `ChannelAdapterPort.get_issue / create_branch / open_pull_request / get_installation_token` | `openbot/application/ports/channel_adapter.py` (C.4) | GitHub adapter (C.4), use case (C.8), E2E recording (C.9) |
| `make_fix_tools(*, sandbox, event, budget=None) -> list[StructuredTool]` | `openbot/infrastructure/agents/_fix_tools.py` (C.6) | Responder (C.7) |
| `DEFAULT_FIX_TOOL_BUDGET = 20` | C.6 | Responder default (C.7), tool tests (C.6) |
| `DeepAgentsFixResponder.fix_for_event(event, *, adapter, sandbox, issue) -> FixOutcome` | `openbot/infrastructure/agents/deepagents_fix.py` (C.7) | Use case (C.8), responder tests (C.7) |
| `PreflightContext.sandbox_factory` | `openbot/application/middleware/preflight.py` (C.8) | Use case (C.8), DI wiring (C.8) |

Method/property names in later tasks must match this table exactly. If a
disagreement is found during implementation, fix the earliest task and
re-run its tests, don't paper over it downstream.

---

## Task C.1: Domain — `FixAttempt` + `FixOutcome`

**Files:**
- Create: `openbot/domain/fix.py`
- Test: `tests/domain/test_fix.py`

Frozen, slot-based dataclasses. No pydantic, no langchain, no HTTP
shapes. Mirrors `openbot/domain/review.py`'s structure (see that file for
why we keep domain pure).

- [ ] **Step 1: Write the failing test file**

```python
# tests/domain/test_fix.py
"""Domain dataclasses for the fix workflow."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from openbot.domain.fix import FixAttempt, FixOutcome


def _attempt(**overrides: object) -> FixAttempt:
    defaults: dict[str, object] = {
        "summary": "fix the off-by-one in pagination",
        "files_changed": ("src/api/list.py",),
        "tests_passed": True,
        "test_command": "pytest tests/",
        "test_output": "3 passed",
        "diff": "diff --git a/src/api/list.py b/src/api/list.py\n",
    }
    defaults.update(overrides)
    return FixAttempt(**defaults)  # type: ignore[arg-type]


def test_attempt_holds_required_fields() -> None:
    a = _attempt()
    assert a.summary == "fix the off-by-one in pagination"
    assert a.files_changed == ("src/api/list.py",)
    assert a.tests_passed is True
    assert a.test_command == "pytest tests/"


def test_attempt_is_frozen() -> None:
    a = _attempt()
    with pytest.raises(FrozenInstanceError):
        a.summary = "no"  # type: ignore[misc]


def test_attempt_files_changed_is_tuple() -> None:
    # Lists would let callers mutate the value after construction.
    a = _attempt(files_changed=("a.py", "b.py"))
    assert isinstance(a.files_changed, tuple)


def test_outcome_holds_attempt_and_optional_pr_url() -> None:
    o = FixOutcome(attempt=_attempt(), pr_url="https://github.com/o/r/pull/9")
    assert o.attempt.tests_passed is True
    assert o.pr_url == "https://github.com/o/r/pull/9"
    assert o.error is None


def test_outcome_defaults_pr_url_and_error_to_none() -> None:
    o = FixOutcome(attempt=_attempt())
    assert o.pr_url is None
    assert o.error is None


def test_outcome_is_frozen() -> None:
    o = FixOutcome(attempt=_attempt())
    with pytest.raises(FrozenInstanceError):
        o.pr_url = "x"  # type: ignore[misc]


def test_outcome_can_record_failure_without_pr() -> None:
    # tests_passed=False is a legitimate terminal state — the use case
    # comments on the issue rather than opening a PR.
    failed = _attempt(tests_passed=False, test_output="1 failed")
    o = FixOutcome(attempt=failed, error=None)
    assert o.pr_url is None
    assert o.attempt.tests_passed is False


def test_outcome_can_record_error_after_passing_tests() -> None:
    # Tests passed but a downstream step (e.g. open_pull_request) raised
    # — error is set, pr_url is None.
    o = FixOutcome(
        attempt=_attempt(),
        pr_url=None,
        error="open_pull_request failed: 422",
    )
    assert o.error is not None
    assert o.pr_url is None
```

- [ ] **Step 2: Verify the test fails**

```bash
make sync   # only the first time per machine
pytest tests/domain/test_fix.py -v
```

Expected: `ModuleNotFoundError: No module named 'openbot.domain.fix'` (or
`ImportError`).

- [ ] **Step 3: Write the domain module**

```python
# openbot/domain/fix.py
"""Pure value objects for the fix workflow — slice C.

Why this lives in the domain layer (mirrors ``openbot/domain/review.py``):

  - The use case (``maybe_run_fix``) decides PR vs comment based on
    ``attempt.tests_passed`` and ``outcome.error`` without touching
    pydantic, langchain, or HTTP shapes.
  - The responder (``DeepAgentsFixResponder``) returns these types, not
    the pydantic schema used to coerce LLM output. Pydantic stops at
    ``openbot/infrastructure/agents/_fix_schema.py``.

Invariants are asserted in tests (not in ``__post_init__``) because
partial outcomes do exist — tests passed but ``open_pull_request`` failed
must still be representable as ``FixOutcome(attempt=..., pr_url=None,
error="...")``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FixAttempt:
    """One reasoning pass through the fix loop.

    Holds everything the use case needs to decide between PR vs comment.

    ``files_changed`` is a tuple (not list) so consumers can't mutate the
    record after the responder returns it. ``test_output`` is the raw
    stdout+stderr from the test run, truncated by the responder to keep
    GitHub comments readable.
    """

    summary: str
    files_changed: tuple[str, ...]
    tests_passed: bool
    test_command: str
    test_output: str
    diff: str


@dataclass(frozen=True, slots=True)
class FixOutcome:
    """End-to-end result of ``maybe_run_fix``.

    ``pr_url`` is set only when ``attempt.tests_passed`` is True and the
    PR was opened successfully. ``error`` is set only when something
    raised before completing — both can be None on the tests-failed path
    (legitimate terminal state).
    """

    attempt: FixAttempt
    pr_url: str | None = None
    error: str | None = None


__all__ = ["FixAttempt", "FixOutcome"]
```

- [ ] **Step 4: Verify the tests pass**

```bash
pytest tests/domain/test_fix.py -v
```

Expected: 8 passed.

- [ ] **Step 5: `make check`**

```bash
make check
```

Expected: all green (formatter, ruff, import-linter, full pytest suite).

- [ ] **Step 6: Commit**

```bash
git add openbot/domain/fix.py tests/domain/test_fix.py
git commit -m "feat(fix): slice C.1 — FixAttempt + FixOutcome domain types"
```

---

## Task C.2: Schema bridge — `_fix_schema.py`

**Files:**
- Create: `openbot/infrastructure/agents/_fix_schema.py`
- Test: `tests/infrastructure/agents/test_fix_schema.py`

Pydantic models the LLM fills in via `response_format`, plus
`parse_structured_response` that coerces the runtime payload into a
domain `FixOutcome`. Mirrors `_review_schema.py` (compare side-by-side
when in doubt — same invariants, same anti-corruption pattern).

- [ ] **Step 1: Write the failing test file**

```python
# tests/infrastructure/agents/test_fix_schema.py
"""Schema bridge tests — pydantic ⇄ domain for the fix responder."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openbot.domain.fix import FixAttempt, FixOutcome
from openbot.infrastructure.agents._fix_schema import (
    FixOutcomeSchema,
    parse_structured_response,
)


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "attempt": {
            "summary": "fix off-by-one",
            "files_changed": ["src/api/list.py"],
            "tests_passed": True,
            "test_command": "pytest -q",
            "test_output": "3 passed in 0.1s",
            "diff": "diff --git a/src/api/list.py b/src/api/list.py\n",
        },
    }
    base.update(overrides)
    return base


def test_validates_minimal_payload() -> None:
    model = FixOutcomeSchema.model_validate(_payload())
    assert model.attempt.summary == "fix off-by-one"
    assert model.attempt.tests_passed is True


def test_to_domain_returns_fix_outcome() -> None:
    model = FixOutcomeSchema.model_validate(_payload())
    domain = model.to_domain()
    assert isinstance(domain, FixOutcome)
    assert isinstance(domain.attempt, FixAttempt)
    # Pydantic gives us a list — bridge must convert to tuple to satisfy
    # the frozen-dataclass field type.
    assert domain.attempt.files_changed == ("src/api/list.py",)
    assert domain.pr_url is None
    assert domain.error is None


def test_rejects_unknown_extras() -> None:
    # ``extra="forbid"`` keeps the agent from sneaking new keys past us.
    bad = _payload()
    bad["unexpected_field"] = "value"
    with pytest.raises(ValidationError):
        FixOutcomeSchema.model_validate(bad)


def test_parse_structured_response_accepts_dict() -> None:
    out = parse_structured_response(_payload())
    assert isinstance(out, FixOutcome)
    assert out.attempt.test_command == "pytest -q"


def test_parse_structured_response_accepts_pydantic_instance() -> None:
    model = FixOutcomeSchema.model_validate(_payload())
    out = parse_structured_response(model)
    assert isinstance(out, FixOutcome)


def test_parse_structured_response_raises_on_garbage() -> None:
    with pytest.raises(
        ValueError, match="deepagents_fix_structured_response_unexpected_type"
    ):
        parse_structured_response(42)  # type: ignore[arg-type]


def test_handles_tests_failed_payload() -> None:
    p = _payload()
    p["attempt"] = {  # type: ignore[dict-item]
        **p["attempt"],  # type: ignore[dict-item]
        "tests_passed": False,
        "test_output": "1 failed",
    }
    out = parse_structured_response(p)
    assert out.attempt.tests_passed is False
    assert out.attempt.test_output == "1 failed"
```

- [ ] **Step 2: Verify the tests fail**

```bash
pytest tests/infrastructure/agents/test_fix_schema.py -v
```

Expected: `ModuleNotFoundError: No module named 'openbot.infrastructure.agents._fix_schema'`.

- [ ] **Step 3: Write the schema module**

```python
# openbot/infrastructure/agents/_fix_schema.py
"""Pydantic schemas for fix responder structured output — slice C.

Why a separate module (same rationale as ``_review_schema.py``):

  - DeepAgents/LangGraph wants a *pydantic* ``response_format`` to coerce
    the agent's final answer into a typed object. Pydantic must not
    cross into the domain layer (CLAUDE.md), so the LLM-facing schema
    lives here and the responder converts to ``FixOutcome`` at the
    boundary.
  - Anti-corruption layer: ``parse_structured_response`` is the single
    chokepoint between LLM output and the use case. Bad payloads fail
    loudly so the responder's outer ``except`` posts the error template
    instead of silently constructing garbage data.

Field meanings match ``openbot/domain/fix.py`` verbatim; that file is
the source of truth.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openbot.domain.fix import FixAttempt, FixOutcome


class _FixAttemptModel(BaseModel):
    """One reasoning pass as the LLM may emit it.

    ``model_config`` forbids extras so the agent can't sneak free-form
    keys past us (e.g., a ``confidence`` we don't yet support). Adding
    fields is a deliberate code change in both schema and domain.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="One-line description of the change.")
    files_changed: list[str] = Field(
        default_factory=list,
        description="Repo-relative paths the agent wrote.",
    )
    tests_passed: bool = Field(
        description="True iff the final test run exited 0."
    )
    test_command: str = Field(
        description="The exact shell command the agent ran for tests."
    )
    test_output: str = Field(
        description="Truncated stdout+stderr from the test run.",
    )
    diff: str = Field(
        default="",
        description="git diff of the working tree after edits.",
    )

    def to_domain(self) -> FixAttempt:
        return FixAttempt(
            summary=self.summary,
            files_changed=tuple(self.files_changed),
            tests_passed=self.tests_passed,
            test_command=self.test_command,
            test_output=self.test_output,
            diff=self.diff,
        )


class _FixOutcomeModel(BaseModel):
    """Top-level fix-loop output the agent fills via ``response_format``.

    ``pr_url`` and ``error`` are unused on the LLM side — the use case
    sets them after the agent returns (when it opens the PR, or when a
    downstream step raises). We keep them in the schema so the same
    pydantic model round-trips through tests.
    """

    model_config = ConfigDict(extra="forbid")

    attempt: _FixAttemptModel
    pr_url: str | None = Field(default=None)
    error: str | None = Field(default=None)

    def to_domain(self) -> FixOutcome:
        return FixOutcome(
            attempt=self.attempt.to_domain(),
            pr_url=self.pr_url,
            error=self.error,
        )


def parse_structured_response(raw: Any) -> FixOutcome:
    """Coerce whatever the agent put in ``result["structured_response"]``
    to a domain ``FixOutcome``.

    DeepAgents may return either a pydantic instance or a plain dict
    depending on langchain version. Both shapes are accepted; anything
    else raises so the responder's outer ``except`` posts the error
    template instead of silently constructing garbage data.
    """
    if isinstance(raw, _FixOutcomeModel):
        return raw.to_domain()
    if isinstance(raw, dict):
        return _FixOutcomeModel.model_validate(raw).to_domain()
    if isinstance(raw, BaseModel):
        return _FixOutcomeModel.model_validate(raw.model_dump()).to_domain()
    raise ValueError(
        f"deepagents_fix_structured_response_unexpected_type:{type(raw).__name__}"
    )


FixOutcomeSchema = _FixOutcomeModel
FixAttemptSchema = _FixAttemptModel


__all__ = [
    "FixAttemptSchema",
    "FixOutcomeSchema",
    "parse_structured_response",
]
```

- [ ] **Step 4: Verify the tests pass**

```bash
pytest tests/infrastructure/agents/test_fix_schema.py -v
```

Expected: 7 passed.

- [ ] **Step 5: `make check`**

```bash
make check
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add openbot/infrastructure/agents/_fix_schema.py \
        tests/infrastructure/agents/test_fix_schema.py
git commit -m "feat(fix): slice C.2 — pydantic schema bridge for FixOutcome"
```

---

**Continue with `2026-05-20-fix-deepagent-slice-c-part2.md`** for task C.3
(SandboxPort growth + FakeSandboxAdapter).
