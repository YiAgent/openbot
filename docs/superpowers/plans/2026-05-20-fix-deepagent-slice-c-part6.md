# Slice C — Fix workflow end-to-end (part 6: DeepAgent fix responder)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Continues from:** `2026-05-20-fix-deepagent-slice-c-part5.md` (`make_fix_tools` factory).
**Continues to:** `2026-05-20-fix-deepagent-slice-c-part7.md` (use case rewrite).

Task C.7 wires DeepAgents + tools + schema into a responder mirroring
`DeepAgentsReviewResponder`. The responder is the only place that talks
to `create_deep_agent`. It must not know about HTTP, Daytona, or PR
opening — those decisions belong to the use case (C.8).

---

## Task C.7: Fix responder — `DeepAgentsFixResponder`

**Files:**
- Create: `openbot/infrastructure/agents/deepagents_fix.py`
- Modify: `openbot/infrastructure/agents/__init__.py` (+1 export)
- Test: `tests/infrastructure/agents/test_deepagents_fix.py`

Per-event responder. Builds tools each call (no caching — tools close
over `(sandbox, event)`; caching would leak sandbox handles across
tenants). Returns a domain `FixOutcome`. The use case decides whether
that outcome becomes a PR or a comment.

### Why this module is small

The responder is intentionally thin — it composes pieces defined in
earlier tasks:

  - Model name: `primary_model_for(Feature.FIX)` (already routed in the
    model_router; `claude-opus-4-7` per PRD §13).
  - Tools: `make_fix_tools(sandbox=..., event=...)` (C.6).
  - Schema: `FixOutcomeSchema` and `parse_structured_response` (C.2).
  - Recursion: 25 (same value used in the review responder — fix loops
    need a touch more headroom because they re-read files and re-run
    tests, but the budget cap on the tool side is 20, so 25 keeps a
    final node for the structured answer).

The responder does *not*:

  - Clone the repo (the use case passes a sandbox already cloned).
  - Open the PR (the use case does that after deciding tests passed).
  - Truncate or wrap test output for GitHub comments (the use case
    does that — it owns the comment templates).

### Reference patterns (read before implementing)

  - `openbot/infrastructure/agents/deepagents_review.py` — the review
    responder is the closest mirror. `_normalize_model_name`,
    `_RECURSION_LIMIT=25`, and `_extract_*` translate one-for-one.
  - `openbot/infrastructure/llm/model_router.py` — provides
    `primary_model_for(Feature.FIX)`. Slice F1 already added the FIX
    feature, so this should not require changes.

### TDD steps

- [ ] **Step 1: Write the failing test file**

```python
# tests/infrastructure/agents/test_deepagents_fix.py
"""DeepAgentsFixResponder — wiring + schema-coercion tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from openbot.domain.events import UnifiedEvent
from openbot.domain.fix import FixAttempt, FixOutcome


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        event_id="evt-1",
        channel="github",
        repo="o/r",
        actor="alice",
        action="issue.assigned",
        pr_number=None,
        issue_number=7,
    )


@dataclass
class _StubAdapter:
    """Adapter is unused by the responder body (the use case handles
    GitHub I/O), but the responder signature accepts one for symmetry
    with ``DeepAgentsReviewResponder``."""


@dataclass
class _StubSandbox:
    """Sandbox is passed through to ``make_fix_tools``; the agent stub
    we monkeypatch never invokes the tools, so this can stay empty."""


@dataclass
class _StubIssue:
    """Mirrors the ``dict`` shape returned by ``adapter.get_issue``."""

    title: str = "Off-by-one on pagination"
    body: str = "Last item is dropped when total % page_size == 0."
    base_sha: str = "abc1234"


def _fake_agent_result(
    *,
    summary: str = "fix off-by-one",
    tests_passed: bool = True,
    test_output: str = "3 passed",
    files_changed: tuple[str, ...] = ("src/api/list.py",),
) -> dict[str, Any]:
    """Shape returned by ``create_deep_agent(...).ainvoke(...)`` when
    ``response_format=FixOutcomeSchema`` is set."""

    return {
        "messages": [],
        "structured_response": {
            "attempt": {
                "summary": summary,
                "files_changed": list(files_changed),
                "tests_passed": tests_passed,
                "test_command": "pytest -q",
                "test_output": test_output,
                "diff": "diff --git a/x b/x\n",
            },
        },
    }


@pytest.mark.asyncio
async def test_returns_fix_outcome_when_agent_succeeds(monkeypatch):
    from openbot.infrastructure.agents import deepagents_fix as mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload, config):
            captured["payload"] = payload
            captured["config"] = config
            return _fake_agent_result()

    def fake_create_deep_agent(*, model, tools, system_prompt, response_format):
        captured["model"] = model
        captured["tool_names"] = [t.name for t in tools]
        captured["response_format"] = response_format
        return FakeAgent()

    monkeypatch.setattr(mod, "create_deep_agent", fake_create_deep_agent)

    responder = mod.DeepAgentsFixResponder()
    outcome = await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={
            "title": _StubIssue().title,
            "body": _StubIssue().body,
            "base_sha": _StubIssue().base_sha,
        },
    )

    assert isinstance(outcome, FixOutcome)
    assert isinstance(outcome.attempt, FixAttempt)
    assert outcome.attempt.tests_passed is True
    assert outcome.attempt.files_changed == ("src/api/list.py",)
    # The schema bridge translates lists → tuples (frozen invariants).

    # Wiring: tool names match the C.6 contract; recursion limit honoured.
    assert captured["tool_names"] == [
        "read_file",
        "write_file",
        "list_files",
        "run_command",
        "git_diff",
        "search_files",
    ]
    assert captured["config"]["recursion_limit"] == 25
    # Schema bridge wiring — pydantic class, not the dict shape.
    assert captured["response_format"].__name__ == "_FixOutcomeModel"


@pytest.mark.asyncio
async def test_includes_issue_context_in_prompt(monkeypatch):
    from openbot.infrastructure.agents import deepagents_fix as mod

    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload, config):
            captured["payload"] = payload
            return _fake_agent_result()

    def fake_create_deep_agent(**_):
        return FakeAgent()

    monkeypatch.setattr(mod, "create_deep_agent", fake_create_deep_agent)

    responder = mod.DeepAgentsFixResponder()
    await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={
            "title": "Off-by-one on pagination",
            "body": "Last item is dropped when total % page_size == 0.",
            "base_sha": "abc1234",
        },
    )

    user_msg = captured["payload"]["messages"][0]["content"]
    assert "Off-by-one on pagination" in user_msg
    assert "page_size == 0" in user_msg
    assert "o/r" in user_msg
    assert "#7" in user_msg
    # The agent should know the base commit so it can ground hashes in
    # tool calls if needed.
    assert "abc1234" in user_msg


@pytest.mark.asyncio
async def test_returns_failure_outcome_when_tests_failed(monkeypatch):
    from openbot.infrastructure.agents import deepagents_fix as mod

    class FakeAgent:
        async def ainvoke(self, payload, config):
            return _fake_agent_result(
                tests_passed=False,
                test_output="1 failed, 2 passed",
            )

    monkeypatch.setattr(
        mod,
        "create_deep_agent",
        lambda **_: FakeAgent(),
    )

    responder = mod.DeepAgentsFixResponder()
    outcome = await responder.fix_for_event(
        _event(),
        adapter=_StubAdapter(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        issue={"title": "t", "body": "b", "base_sha": "abc1234"},
    )

    # Tests-failed is a *legitimate* terminal outcome — the responder
    # just reports the attempt; the use case decides comment vs PR.
    assert outcome.attempt.tests_passed is False
    assert outcome.pr_url is None
    assert outcome.error is None
    assert "1 failed" in outcome.attempt.test_output


@pytest.mark.asyncio
async def test_raises_when_structured_response_missing(monkeypatch):
    from openbot.infrastructure.agents import deepagents_fix as mod

    class FakeAgent:
        async def ainvoke(self, payload, config):
            return {"messages": []}  # no structured_response key

    monkeypatch.setattr(
        mod,
        "create_deep_agent",
        lambda **_: FakeAgent(),
    )

    responder = mod.DeepAgentsFixResponder()
    with pytest.raises(
        ValueError, match="deepagents_fix_result_missing_structured_response"
    ):
        await responder.fix_for_event(
            _event(),
            adapter=_StubAdapter(),  # type: ignore[arg-type]
            sandbox=_StubSandbox(),  # type: ignore[arg-type]
            issue={"title": "t", "body": "b", "base_sha": "abc1234"},
        )
```

- [ ] **Step 2: Verify the tests fail**

```bash
pytest tests/infrastructure/agents/test_deepagents_fix.py -v
```

Expected: `ModuleNotFoundError: No module named 'openbot.infrastructure.agents.deepagents_fix'`.

- [ ] **Step 3: Write the responder module**

```python
# openbot/infrastructure/agents/deepagents_fix.py
"""DeepAgent-backed fix responder — slice C.

What it owns:

  - Build a per-event DeepAgent with tools that close over a live
    sandbox handle (see C.6 — ``make_fix_tools``).
  - Pass the issue body to the model and read back the structured
    answer via ``response_format=FixOutcomeSchema``.
  - Convert the pydantic object to a domain ``FixOutcome`` and return
    it. Nothing else.

What it does NOT own:

  - GitHub PR creation, branch creation, push (use case).
  - Sandbox lifecycle (the use case creates and closes the sandbox).
  - Issue fetching (the use case fetches via the channel adapter).
  - Comment templating for failures (the use case owns the templates).

This separation matches ``deepagents_review.py``: the responder is a
pure ``UnifiedEvent + side-effecting tools → FixOutcome`` function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents import create_deep_agent

from openbot.application.ports.sandbox import SandboxPort
from openbot.domain.events import UnifiedEvent
from openbot.domain.fix import FixOutcome
from openbot.infrastructure.agents._fix_schema import (
    FixOutcomeSchema,
    parse_structured_response,
)
from openbot.infrastructure.agents._fix_tools import make_fix_tools
from openbot.infrastructure.llm.model_router import Feature, primary_model_for

if TYPE_CHECKING:
    from openbot.application.ports.channel_adapter import ChannelAdapterPort

# Same value used by the review responder. LangGraph counts every node
# visit; a fix loop with 20 tool calls visits ~50 nodes (20 ToolCall +
# 20 ToolMessage + alternating LLM nodes), so the budget is the real cap.
# 25 here is a defensive ceiling against unbounded reasoning loops the
# tool-side budget can't see.
_RECURSION_LIMIT = 25

_SYSTEM_PROMPT = """You are OpenBot, a senior engineer. You will fix the bug \
described in the GitHub issue below by editing files in the sandbox and \
running tests until they pass. Return a JSON object matching the schema — \
never plain text.

Workflow:
- Read the issue carefully. Form a hypothesis about which file(s) are wrong.
- Use `list_files` and `search_files` to navigate; use `read_file` to inspect.
- Use `write_file` to apply the smallest possible change that fixes the bug.
- Use `run_command` to run the project's test suite. You pick the command \
(e.g. `pytest -q`, `npm test`, `go test ./...`) based on what you see in the repo.
- If tests fail, iterate: re-read code, refine the fix, re-run tests.
- When tests pass, use `git_diff` to capture the final diff and return your structured answer.

Tools available (total tool calls are budget-capped — stop iterating before you exhaust):
- `read_file(path)` — read a UTF-8 file from the sandbox working tree.
- `write_file(path, content)` — overwrite or create a file. Always read first when modifying.
- `list_files(path=".")` — list a directory's entries (non-recursive).
- `run_command(command)` — run a shell command in the sandbox. Use this for tests and inspections.
- `git_diff()` — return `git diff` against the base commit. Call this once near the end.
- `search_files(pattern, path_glob="**/*")` — recursive grep (regex) in the working tree.

Rules:
- Make the smallest change that fixes the bug. Do not refactor unrelated code.
- Tests must pass on the final attempt — set `tests_passed=false` only if you \
genuinely could not fix it within your budget.
- Use the project's existing test runner. If you cannot detect one, default to \
`pytest -q` for Python repos.
- Return ONE structured object. Do not emit chains of thought, multi-turn \
dialogue, or markdown prose outside the schema.
- Keep `summary` to one line.
- `files_changed` lists the repo-relative paths you wrote.
- `test_output` should be the tail of the final test run (truncate yourself \
to ~2000 chars; the use case will truncate further if needed for GitHub).
"""


def _normalize_model_name(model: str) -> str:
    """Map ``provider/name`` (LiteLLM) → ``provider:name`` (langchain_litellm).

    Same helper as ``deepagents_review.py``. Duplicated rather than
    shared because the rule is small and keeping responders independent
    helps when one needs to evolve faster than the other.
    """
    if ":" in model:
        return model
    if "/" in model:
        provider, name = model.split("/", 1)
        return f"{provider}:{name}"
    return model


def _user_prompt(
    event: UnifiedEvent,
    *,
    issue_title: str,
    issue_body: str,
    base_sha: str,
) -> str:
    """Format the user-turn prompt for the fix agent.

    Issue title and body are passed as plain text — the agent reads them
    to form a hypothesis. ``base_sha`` is included so the agent has a
    stable reference point for ``git_diff`` and so tool calls that
    reference commits can ground themselves.
    """
    body = issue_body.strip() or "(no description provided)"
    return (
        "GitHub context:\n"
        f"- repository: {event.repo}\n"
        f"- issue: #{event.issue_number}\n"
        f"- actor: {event.actor}\n"
        f"- base commit: {base_sha}\n\n"
        f"Issue title: {issue_title}\n\n"
        "Issue body:\n"
        f"{body}\n\n"
        "Fix the bug. Run the project's tests until they pass. Return one "
        "structured object matching the schema."
    )


def _extract_outcome(result: dict[str, Any]) -> FixOutcome:
    """Read DeepAgents' structured-output channel and coerce to the domain type.

    Mirrors ``_extract_findings`` in ``deepagents_review.py``: structured
    response is the contract; missing it is a programmer error, not a
    user error, so we raise rather than returning a default.
    """
    structured = result.get("structured_response")
    if structured is None:
        raise ValueError("deepagents_fix_result_missing_structured_response")
    return parse_structured_response(structured)


class DeepAgentsFixResponder:
    """Stateless fix responder — a fresh agent is built per call.

    Tools close over ``(sandbox, event)`` so the agent must be rebuilt
    per call. Caching by model alone would let a previous tenant's
    sandbox handle leak into the next event — a multi-tenant correctness
    bug, not a perf issue.
    """

    async def fix_for_event(
        self,
        event: UnifiedEvent,
        *,
        adapter: ChannelAdapterPort,
        sandbox: SandboxPort,
        issue: dict[str, Any],
    ) -> FixOutcome:
        """Run the fix loop and return a domain outcome.

        ``adapter`` is accepted for signature symmetry with
        ``DeepAgentsReviewResponder`` and to leave room for future
        in-loop GitHub calls (e.g. fetching related issues). Today the
        responder does not call it; the use case does the I/O.

        ``issue`` is the dict shape returned by
        ``ChannelAdapterPort.get_issue`` (see C.4). The responder reads
        ``title``, ``body``, and ``base_sha`` only; extra keys are
        ignored so the dict can grow without breaking this signature.
        """
        del adapter  # unused — see docstring
        if event.issue_number is None:
            raise ValueError("deepagents_fix_requires_issue_number")
        tools = make_fix_tools(sandbox=sandbox, event=event)
        agent = create_deep_agent(
            model=_normalize_model_name(primary_model_for(Feature.FIX)),
            tools=tools,
            system_prompt=_SYSTEM_PROMPT,
            response_format=FixOutcomeSchema,
        )
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": _user_prompt(
                            event,
                            issue_title=str(issue.get("title", "")),
                            issue_body=str(issue.get("body", "")),
                            base_sha=str(issue.get("base_sha", "")),
                        ),
                    }
                ]
            },
            config={"recursion_limit": _RECURSION_LIMIT},
        )
        return _extract_outcome(result)


__all__ = ["DeepAgentsFixResponder"]
```

- [ ] **Step 4: Add the export**

Open `openbot/infrastructure/agents/__init__.py` and add the export.
The file already exports `DeepAgentsReviewResponder` from slice B — add
the fix one next to it so the public surface stays alphabetical and
grouped by responder type. Do not re-export private names
(`_FixAttemptModel`, `_FixOutcomeModel`).

```python
# openbot/infrastructure/agents/__init__.py
"""Infrastructure-layer agent implementations.

Public surface is intentionally narrow: the use cases import responders
by class name and nothing else. Internal helpers (``_fix_schema``,
``_fix_tools``, ``_review_schema``, ``_review_tools``) stay private to
the package.
"""

from openbot.infrastructure.agents.deepagents_fix import DeepAgentsFixResponder
from openbot.infrastructure.agents.deepagents_review import DeepAgentsReviewResponder

__all__ = [
    "DeepAgentsFixResponder",
    "DeepAgentsReviewResponder",
]
```

(If the existing `__init__.py` differs — e.g. already exports more —
preserve everything it currently exports and *add* `DeepAgentsFixResponder`
to both the import block and `__all__`. Do not delete existing exports.)

- [ ] **Step 5: Verify the tests pass**

```bash
pytest tests/infrastructure/agents/test_deepagents_fix.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Verify the FIX feature exists in the model router**

```bash
python -c "from openbot.infrastructure.llm.model_router import Feature, primary_model_for; print(Feature.FIX, primary_model_for(Feature.FIX))"
```

Expected output (per PRD §13 #2):

```
Feature.FIX claude-opus-4-7
```

If this prints `AttributeError: FIX` or returns an unexpected model
name, the router is out of date — stop and add the FIX feature there
before continuing. Per PRD §13 #2 (locked model routing), FIX must
route to `claude-opus-4-7`.

- [ ] **Step 7: `make check`**

```bash
make check
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add openbot/infrastructure/agents/deepagents_fix.py \
        openbot/infrastructure/agents/__init__.py \
        tests/infrastructure/agents/test_deepagents_fix.py
git commit -m "feat(fix): slice C.7 — DeepAgentsFixResponder with structured outcome"
```

---

## Notes for reviewers (C.7 only)

1. **The responder is a glue file**. Almost every interesting line is
   a delegation: schema (`_fix_schema`), tools (`_fix_tools`), model
   routing (`model_router`), structured-output coercion
   (`parse_structured_response`). Resist the urge to expand its body —
   if you find yourself adding HTTP logic or branch-name logic here,
   that belongs in the use case (C.8).

2. **Why we accept `adapter` we don't use.** Two reasons. (a) Signature
   symmetry with `DeepAgentsReviewResponder.review_for_event(event, *,
   adapter)` makes the responder pair feel like a regular pattern, not
   two ad-hoc functions. (b) Today's fix loop doesn't fetch related PRs
   or issues from inside the agent loop; tomorrow's might. Accepting
   the port today avoids breaking the signature when we add that later.

3. **No tool-budget knob on the responder.** The default budget (20)
   is set in `_fix_tools.py` and applied per call. If a future tenant
   needs a higher budget for monorepos, expose it through
   `make_fix_tools(*, budget=...)` and have the use case pass it down;
   don't add a parameter on the responder for it. The responder stays
   shape-stable.

4. **System prompt fits the schema.** The prompt explicitly tells the
   model to "return a JSON object matching the schema — never plain
   text." That phrasing matches the review responder's prompt, which we
   know works with `claude-opus-4-7 + response_format`. If you change
   the schema, update the prompt's tool list and field hints to match
   in the same commit.

5. **No prompt-quality assertions in `tests/`.** The tests above
   verify *wiring* (tool names, recursion limit, schema-class identity,
   prompt context strings). They do NOT assert anything about prompt
   wording quality (CLAUDE.md §forbidden: "Do not put LLM-behavior or
   prompt-quality assertions in `tests/` — those belong in `evals/`").
   Prompt-quality lives in `evals/` per PRD §8.3.

---

**Continue with `2026-05-20-fix-deepagent-slice-c-part7.md`** for task C.8
(use case rewrite + `PreflightContext.sandbox_factory`).
