# Review / Fix DeepAgent Integration — Plan

> **Status:** DRAFT — surfacing design decisions before implementation.
> Implementation tasks (slices A/B/C) are concrete TDD checklists, but the
> four **Open Questions** in §2 must be answered first because they change
> the surface area of slice C (Fix).

**Date:** 2026-05-20
**Branch (target):** `feat/review-fix-deepagent`
**Tech stack:** Python 3.12, deepagents, LiteLLM, LangSmith, pytest-asyncio
**Related plans:**
- `2026-05-20-webhook-worker-layering-f3-part1.md` — TaskSpec F3 fields (already shipped)
- `2026-05-20-webhook-worker-layering-f3-part3.md` — wiring + acceptance (already shipped)
- `docs/_archive/webhook-worker/openbot-harness-spec.md` §1.2 (archived) — original sandbox decision

---

## 1. Goal & Scope

**Goal:** Port the `DeepAgentsChatResponder` pattern from
`openbot/infrastructure/agents/deepagents_chat.py` to the `review` and `fix`
workflows so they emit a real LLM-driven reply instead of the
`_ACK_TEMPLATE` stub.

**In scope (this plan):**

1. `DeepAgentsReviewResponder` — read-only tools (PR diff, file fetch, grep)
2. `DeepAgentsFixResponder` — tools above + write-file + (maybe) run-tests
3. Plumbing F3 hints (`is_incremental`, `classifier_output`,
   `stages_to_run`) through `PreflightContext` so review can honour them
4. New `evals/` cells: review quality + fix quality (lives outside `tests/`
   per CLAUDE.md "no LLM-behavior assertions in tests/")

**Out of scope (deferred):**

- `evals.sandboxes.factory` integration for Fix — see Open Question #2
- Cross-workflow Sandbox Port wiring (the Protocol in
  `application/ports/sandbox.py` stays unimplemented until Fix lands)
- Branch creation / PR opening for Fix output — slice C only generates the
  patch and replies; PR opening lands in v0.2

---

## 2. Open Questions (need user decisions before slice C)

### Q1. Tool surface for Review

Review reads but never writes. Three candidate tool sets:

| Option | Tools | Pro | Con |
|--------|-------|-----|-----|
| **A. Minimal** | `get_pr_diff`, `get_pr_metadata` | Cheapest tokens; one HTTP call. | Agent can't follow up on unfamiliar identifiers. |
| **B. Standard (recommended)** | A + `read_file(path, ref)`, `grep_repo(pattern, glob)` | Matches Cursor / Aider review UX. | Two extra GitHub API calls per turn × N turns ⇒ budget tracking matters. |
| **C. Full** | B + `web_fetch(url)` | Agent can read linked RFCs / issues. | Network egress + LangSmith trace bloat. |

Default if unanswered: **B**.

### Q2. Sandbox for Fix

PRD `CLAUDE.md` locks `evals.sandboxes.factory` under `evals/`. The
`application/ports/sandbox.py` Protocol is stubbed. Three paths:

| Option | What ships | When |
|--------|-----------|------|
| **A. Defer Fix to v0.2** | Slice C generates a markdown patch suggestion only (no execution); user copy-pastes. | This plan = review-only; Fix becomes a v0.2 plan. |
| **B. No-sandbox Fix** | Fix runs `git apply` inside the worker process. **Dangerous** — no isolation; LLM has shell. | Not recommended — violates PRD §3 locked boundary. |
| **C. Lift `evals.sandboxes.factory` to `infrastructure/`** | Move Daytona/Docker adapters under `infrastructure/sandbox/`, wire `SandboxPort` to them, refactor `evals/` to import from `infrastructure/`. | This plan adds slice C2 (~1 day refactor). |

Recommended: **A (defer Fix)**. Ship review now, add Fix slice later when
sandbox lift is its own focused PR.

### Q3. F3 hint plumbing

F3 stores `is_incremental` / `classifier_output` / `stages_to_run` on
`TaskSpec` but `PreflightContext` doesn't carry them. Review needs at
least `is_incremental` and `classifier_output` to:

- Skip the diff fetch when `is_incremental` and no new code (force_push
  edge case)
- Mention the classifier's `severity_guess` in the review preamble

Two options:

| Option | Change | Blast radius |
|--------|--------|--------------|
| **A. Add fields to `PreflightContext`** (recommended) | Add `task_spec: TaskSpec \| None` to `PreflightContext`; worker passes it in. | One dataclass field + one assignment in `execute_handler`. |
| **B. Re-derive from `event.raw`** | `review.py` re-runs `compute_diff_scope` + classifier. | Wasteful (already computed in webhook segment) and breaks symmetry. |

Default if unanswered: **A**.

### Q4. LangSmith trace granularity

`@_traceable(run_type="chain", name="review")` already wraps
`maybe_run_review`. The new DeepAgent will emit its own LiteLLM spans.

Options:

| Option | Trace shape | Operator UX |
|--------|------------|-------------|
| **A. Wrap responder in `chain`** (recommended) | One workflow span → one DeepAgent span → N LLM spans. | Matches chat; cost per workflow is one query. |
| **B. Tools-only trace** | Each tool call is its own LangSmith run. | Loses workflow-level cost rollup. |

Default if unanswered: **A** (chat already does this).

---

## 3. Current State (verified by code reading, not memory)

| Surface | Current code | Comment |
|---------|--------------|---------|
| `openbot/application/use_cases/review.py` | Stub posting `_ACK_TEMPLATE`, wrapped in `audit_lifecycle`. ~75 lines. | Replace `_ACK_TEMPLATE` with `_RESPONDER.review_for_event(...)`; keep audit + error fallback. |
| `openbot/application/use_cases/fix.py` | Stub posting `_ACK_TEMPLATE`. ~60 lines. | Untouched if Q2 = A. |
| `openbot/infrastructure/agents/deepagents_chat.py` | Real impl: `create_deep_agent(model=…, tools=[], system_prompt=…)` + `lru_cache(4)`. | Template for the two new responders. |
| `openbot/application/ports/sandbox.py` | `SandboxPort` Protocol, no adapter wired. | Stays unimplemented; first impl follows the Fix slice. |
| `openbot/dispatcher/incremental.py` | `compute_diff_scope` returns `DiffScope(is_incremental, is_force_push, …)`. | Already shipped in F3. |
| `openbot/infrastructure/queue/worker.py:218-228` | Persists `head_sha` to `task_runs.last_reviewed_sha` after a completed review. | Already shipped — relevant for incremental review. |
| `openbot/infrastructure/queue/task_spec.py` | Carries `classifier_output`, `is_incremental`, `is_force_push`, `stages_to_run`. | Shipped. **Not** forwarded to `PreflightContext` yet — see Q3. |

---

## 4. Slice A — `DeepAgentsReviewResponder` (read-only, no sandbox)

**Goal:** Wire a DeepAgent reply to `maybe_run_review`. Tool set = Q1's
chosen option (default B). No F3 hints yet (slice B adds those).

**Files:**

- Create: `openbot/infrastructure/agents/deepagents_review.py`
- Modify: `openbot/infrastructure/agents/__init__.py` (export the class)
- Modify: `openbot/application/use_cases/review.py`
- Create: `tests/infrastructure/agents/test_deepagents_review.py`
- Create: `tests/application/use_cases/test_review_deepagent_wiring.py`

### Step 1 — Skeleton + test scaffolding

- [ ] Create `tests/infrastructure/agents/test_deepagents_review.py`:

```python
"""Wiring tests — DeepAgentsReviewResponder.

Asserts the tool surface, prompt assembly, and cache behaviour. Does NOT
assert LLM output quality — that lives in evals/.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.agents.deepagents_review import (
    DeepAgentsReviewResponder,
)


def _pr_event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d1",
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
                "title": "Refactor parser",
                "body": "Cleans up edge cases",
                "head": {"sha": "HEADSHA"},
                "base": {"sha": "BASESHA"},
            }
        },
    )


@pytest.mark.asyncio
async def test_review_invokes_agent_with_pr_context() -> None:
    fake_agent = MagicMock()
    fake_agent.ainvoke = AsyncMock(
        return_value={
            "messages": [MagicMock(content="No blocking issues found.")]
        }
    )
    with patch(
        "openbot.infrastructure.agents.deepagents_review._agent_for_model",
        return_value=fake_agent,
    ):
        responder = DeepAgentsReviewResponder(adapter=MagicMock())
        reply = await responder.review_for_event(_pr_event())

    assert "No blocking issues" in reply
    call = fake_agent.ainvoke.await_args.args[0]
    prompt = call["messages"][0]["content"]
    assert "org/repo" in prompt
    assert "#42" in prompt
    assert "HEADSHA" in prompt
```

- [ ] Run to confirm FAIL (module does not exist):
  `python -m pytest tests/infrastructure/agents/test_deepagents_review.py -v`

### Step 2 — Implement the responder

- [ ] Create `openbot/infrastructure/agents/deepagents_review.py` (~150
      lines target, hard cap 250). Skeleton:

```python
"""DeepAgent-backed PR review responder."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from deepagents import create_deep_agent

from openbot.application.ports.channel_adapter import ChannelAdapterPort
from openbot.domain.events import UnifiedEvent
from openbot.domain.workflows import Feature
from openbot.infrastructure.llm.model_router import primary_model_for

_SYSTEM_PROMPT = """You are OpenBot, a senior code reviewer reviewing one
GitHub pull request.

Rules:
- Comment only on issues you can justify from the diff or file content.
- Prefer "blocking" / "nit" / "praise" tags.
- Skip drive-by suggestions unrelated to the diff.
- If you cannot read a file via the tools, say so — never fabricate.
"""


def _normalize_model_name(model: str) -> str:
    if ":" in model:
        return model
    if "/" in model:
        provider, name = model.split("/", 1)
        return f"{provider}:{name}"
    return model


def _build_tools(adapter: ChannelAdapterPort, event: UnifiedEvent) -> list[Any]:
    """Tool set — see plan §2 Q1. Default = option B (standard)."""
    # Each tool is a thin async closure over the adapter + event.
    # Implementations live alongside this module to keep adapter coupling local.
    raise NotImplementedError  # filled in by step 2.b


def _user_prompt(event: UnifiedEvent) -> str:
    pr = event.raw.get("pull_request") or {}
    return (
        f"Review PR #{event.pr_number} in {event.repo}.\n"
        f"Title: {pr.get('title', '(no title)')}\n"
        f"Author: @{event.actor}\n"
        f"head SHA: {(pr.get('head') or {}).get('sha')}\n"
        f"base SHA: {(pr.get('base') or {}).get('sha')}\n\n"
        "Use the provided tools to read the diff and any files you need, "
        "then post one consolidated review comment."
    )


def _extract_reply(result: dict[str, Any]) -> str:
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("deepagents_review_missing_messages")
    last = messages[-1]
    content = getattr(last, "content", None) or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    text = str(content).strip()
    if not text:
        raise ValueError("deepagents_review_empty_reply")
    return text


@lru_cache(maxsize=4)
def _agent_for_model(model: str, tools_signature: tuple):
    # tools_signature is part of the cache key so per-event tools still
    # share an agent when the surface is identical (model + tool names).
    return create_deep_agent(
        model=_normalize_model_name(model),
        tools=[],  # placeholder; step 2.b wires real tools
        system_prompt=_SYSTEM_PROMPT,
    )


class DeepAgentsReviewResponder:
    def __init__(self, *, adapter: ChannelAdapterPort) -> None:
        self._adapter = adapter

    async def review_for_event(self, event: UnifiedEvent) -> str:
        tools = _build_tools(self._adapter, event)
        agent = _agent_for_model(
            primary_model_for(Feature.REVIEW),
            tuple(getattr(t, "name", repr(t)) for t in tools),
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": _user_prompt(event)}]}
        )
        return _extract_reply(result)


__all__ = ["DeepAgentsReviewResponder"]
```

- [ ] Step 2.b — implement `_build_tools` for the chosen option from Q1.
  Each tool is `async def` returning a string the agent consumes; use
  `@_traceable(run_type="tool", name=…)` so LangSmith picks them up.

- [ ] Add `DeepAgentsReviewResponder` to
  `openbot/infrastructure/agents/__init__.py` exports.

- [ ] Run unit test until green.

### Step 3 — Wire into `review.py`

- [ ] In `openbot/application/use_cases/review.py`:
  - Replace `_ACK_TEMPLATE` block with
    `message = await _RESPONDER.review_for_event(event)`.
  - Instantiate `_RESPONDER = DeepAgentsReviewResponder(adapter=…)` —
    but `adapter` lives on `ctx`, so the responder takes the adapter at
    call-time (`DeepAgentsReviewResponder().review_for_event(event,
    adapter=ctx.adapter)`) or we keep a module-level singleton **without**
    adapter and pass adapter into `review_for_event`. The latter mirrors
    chat; pick it.
  - Wrap in the existing try/except — on failure post a friendly error
    template (mirror chat's `_ERROR_TEMPLATE`).
  - Keep the `audit_lifecycle` block and `outcome` assignment.

- [ ] Add `tests/application/use_cases/test_review_deepagent_wiring.py`
  asserting: success path posts the responder reply; responder exception
  triggers fallback message + `audit_lifecycle` records `FAILED`.

### Step 4 — Make-check + commit

- [ ] `make check` green (lint + tests).
- [ ] Commit:
  `feat(review): wire DeepAgents responder; tools= <option-B/...>`

---

## 5. Slice B — F3 hint plumbing through `PreflightContext`

Skip this slice if Q3 is answered **B** (re-derive). Default **A**.

**Files:**

- Modify: `openbot/application/middleware/preflight.py` (add field)
- Modify: `openbot/application/dispatcher.py` (`execute_handler` accepts spec)
- Modify: `openbot/infrastructure/queue/worker.py` (pass `spec`)
- Modify: `openbot/application/use_cases/review.py` (use `ctx.task_spec`)
- Test: `tests/application/test_execute_handler_carries_task_spec.py`

### Step 1 — Failing test

- [ ] Test asserts that `PreflightContext` constructed from a `TaskSpec`
  with `is_incremental=True, classifier_output={"type":"bug"}` carries
  those through to the workflow handler.

### Step 2 — Implementation

- [ ] Add `task_spec: TaskSpec | None = None` to `PreflightContext`
  (frozen-dataclass field, default None to keep existing call sites
  working).
- [ ] `execute_handler` signature gains
  `task_spec: TaskSpec | None = None`; passes it to `PreflightContext`.
- [ ] Worker passes `spec` into `execute_handler(..., task_spec=spec)`.
- [ ] Review responder accepts `task_spec` and, when present, prepends a
  preamble to the user prompt describing
  `is_incremental` / `classifier_output.severity_guess`.

### Step 3 — Tests + commit

- [ ] Unit test for the prompt-preamble branch (mock the agent, assert
  preamble text appears in the dispatched prompt).
- [ ] `make check`.
- [ ] Commit:
  `feat(preflight): forward TaskSpec to handlers; review consumes F3 hints`

---

## 6. Slice C — Fix DeepAgent (conditional on Q2)

**If Q2 = A (defer):** This slice is a separate v0.2 plan; this PR ends
after slice B.

**If Q2 = C (lift sandbox factory):** Add the slices below. **Do not
adopt option B (no-sandbox)** — violates PRD §3.

### Slice C1 — Lift sandbox factory to `infrastructure/`

- [ ] Move `evals/sandboxes/{factory,daytona_backend,docker_backend,modal_backend,repo_setup}.py`
  → `openbot/infrastructure/sandbox/`.
- [ ] Provide a re-export shim under `evals/sandboxes/__init__.py` so
  eval harnesses keep importing from the old path during transition.
- [ ] Wire `SandboxPort` adapter in `infrastructure/sandbox/adapter.py`
  that implements the Protocol in `application/ports/sandbox.py`.
- [ ] Update `OPENBOT_SANDBOX_BACKEND` documentation in README — note
  the new dual-use surface.
- [ ] Tests live under `tests/infrastructure/sandbox/`.

### Slice C2 — `DeepAgentsFixResponder`

- [ ] Mirror `DeepAgentsReviewResponder` but with tools:
  `read_file`, `write_file`, `run_command(cmd, timeout)` — the last
  goes through `SandboxPort.run`.
- [ ] System prompt enforces "produce a unified diff in the final
  message" so we can extract a patch from `_extract_reply`.
- [ ] Reply format: post the patch as a markdown fenced block; do NOT
  open a PR yet (deferred to v0.2).
- [ ] Tests: tool surface, sandbox call counts, patch extraction.

### Slice C3 — Wire into `fix.py`

- [ ] Replace `_ACK_TEMPLATE.format(...)` with the responder call inside
  `audit_lifecycle`.

---

## 7. Acceptance Checks

After each slice:

1. `make check` — full ruff + pytest green; no skipped tests.
2. `python -m pytest tests/e2e/test_spec_demos.py -v` — the 18 E2E
   scenarios still pass (this plan does not change the dispatcher).
3. `python -m pytest tests/infrastructure/agents/ -v` — new wiring
   tests cover tool surface, prompt content, cache behaviour, error
   fallback.
4. Manual smoke (optional): run the worker locally with `make dev`
   against a personal repo, open a PR, confirm a DeepAgent review
   comment appears.

**Evals (separate PR, lands in `evals/`):**

- `evals/review/` cell — 10 hand-picked PRs with expected
  "should-block" / "nit" / "approve" verdicts. Scored by
  precision@blocking + manual rubric per PRD §8.3.
- `evals/fix/` cell — only if slice C ships.

---

## 8. Risks & Open Issues

1. **DeepAgents tool latency** — each tool call is one HTTP roundtrip
   through `ChannelAdapterPort`. A chatty agent on a 20-file PR could
   spike LLM cost. Mitigation: `evals/` measures `tool_call_count`
   per task and gates regressions; `CostMeter` tracks $ via existing
   middleware.
2. **F3 hint backward compat** — adding `task_spec: TaskSpec | None` to
   `PreflightContext` is opt-in (default None) so existing webhook-path
   tests pass unchanged. Verified by reading `preflight.py:97-124`.
3. **Sandbox lift scope (slice C1)** — moving `evals/sandboxes/` is a
   ~300-line move + import-rewrite. Doable but should land as its own
   PR before slice C2.
4. **LangSmith trace inflation** — DeepAgent emits per-tool spans. Q4
   default (A) keeps the workflow-level rollup intact; if Q4 = B we
   should add a `langsmith.run_helpers.trace` context manager around
   the responder call.

---

## 9. Locked Decisions (do not re-litigate)

- **No LLM-behavior assertions in `tests/`** — quality evals live in
  `evals/` per CLAUDE.md.
- **`v0.1` feature set stays `triage+review+fix+chat`** — this plan
  upgrades review (and possibly fix), nothing else.
- **GitHub-only channel** — no Slack/Discord/Linear in this plan.
- **LangSmith is the only tracer** — do not introduce Langfuse here.
- **Model routing per PRD §13 #2** — review/fix → `claude-opus-4-7`,
  not configurable per request.

---

## 10. Next Action

Answer Open Questions §2 (Q1–Q4) before slice A begins. Q2 has the
largest impact (defer-vs-lift). Suggested defaults:

| Q | Default | Why |
|---|---------|-----|
| Q1 | B (standard tools) | Matches industry baseline; lets `evals/` measure tool-call budget. |
| Q2 | **A (defer Fix)** | Keeps this PR ~500 LOC; sandbox lift becomes its own focused PR. |
| Q3 | A (add to `PreflightContext`) | One-line dataclass change; avoids re-deriving incremental scope. |
| Q4 | A (chain wrap) | Matches chat trace shape; gives one cost rollup per workflow run. |
