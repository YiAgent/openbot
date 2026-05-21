# Slice C — Fix workflow end-to-end (part 5: fix tools factory)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Picks up from:** part 4 (C.5 Daytona sandbox adapter).
**Continues in:** part 6 (C.7 responder + C.8 use case), part 7 (C.9 E2E demo + finalization).

This part lands the per-event tool factory the fix responder will plug
into. Mirrors slice A2's `_review_tools.py` shape, with three deliberate
differences:

- Higher default budget (20 instead of 5) — fix runs need more headroom.
- Includes write_file + run_command — review tools are read-only.
- `search_files` uses sandbox-local `grep` rather than GitHub Code Search,
  because the agent works inside a freshly-cloned tree.

---

## Task C.6: `_fix_tools.py` (per-event StructuredTool factory)

**Files:**
- Create: `openbot/infrastructure/agents/_fix_tools.py`
- Test: `tests/infrastructure/agents/test_fix_tools.py`

### Why a separate budget from review's 5

Review tools are read-only (`read_file`, `grep_repo`) and almost always
converge in 3-5 calls. Fix tools are read/write: the agent typically
runs `list_files` (1) → `read_file` × several (3-6) → `write_file` × M
(1-3) → `run_command` × K to find and rerun tests (2-5). 5 is far too
tight; 20 gives comfortable headroom while still catching a runaway
loop. Tests freeze the constant so a bump is intentional.

### Why no GitHub-touching tool

Branch creation and PR opening are the use case's job, not the agent's.
The agent decides *what* to change and *how* to run tests; the
deterministic post-processing (push, open PR, post comment) lives in
`maybe_run_fix` where the test outcome is known. Keeping the agent away
from network side effects also matches the per-event sandbox isolation
boundary.

### Why `search_files` is sandbox-local, not GitHub Code Search

The review agent's `grep_repo` hits GitHub Code Search (cross-repo
relevance). The fix agent works inside one cloned tree where a local
`grep` is both faster and consistent with the just-cloned ref. The
tool wraps `sandbox.run(["grep", "-rn", pattern, ...])`.

---

### Step 1: Write the failing fix-tools tests

- [ ] **Step 1.1: Create `tests/infrastructure/agents/test_fix_tools.py`.**

```python
"""make_fix_tools — per-event tool factory for DeepAgentsFixResponder.

These tests use a hand-rolled StubSandbox that satisfies SandboxPort,
because we want to assert tool↔sandbox wiring without dragging in either
the fake (subprocess + tempdir) or Daytona (MagicMock) machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from openbot.application.ports.sandbox import ExecResult
from openbot.domain.events import EventKind, UnifiedEvent
from openbot.infrastructure.agents._fix_tools import (
    DEFAULT_FIX_TOOL_BUDGET,
    ToolBudget,
    ToolBudgetExceededError,
    make_fix_tools,
)


@dataclass
class _StubSandbox:
    """Minimal SandboxPort impl that records calls and replays canned results."""

    workspace: str = "/workspace/repo"
    files: dict[str, str] = field(default_factory=dict)
    run_calls: list[list[str]] = field(default_factory=list)
    run_result: ExecResult = field(
        default_factory=lambda: ExecResult(
            stdout="", stderr="", exit_code=0, timed_out=False
        )
    )
    listing: list[str] = field(default_factory=list)
    diff_text: str = ""

    async def clone(self, *, repo_url: str, ref: str, token: str) -> None:
        raise AssertionError("tools must not call clone()")

    async def read_file(self, path: str) -> str:
        return self.files.get(path, "")

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def list_files(self, *, path: str = ".", max: int = 200) -> list[str]:
        return list(self.listing)

    async def run(
        self,
        *,
        command: list[str],
        timeout_seconds: int = 60,
        env: Any = None,
    ) -> ExecResult:
        self.run_calls.append(command)
        return self.run_result

    async def git_diff(self) -> str:
        return self.diff_text

    async def commit_and_push(
        self, *, branch_ref: str, message: str, token: str
    ) -> None:
        raise AssertionError("tools must not call commit_and_push()")

    async def close(self) -> None:  # pragma: no cover
        raise AssertionError("tools must not call close()")


def _event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="d",
        kind=EventKind.ISSUE_ASSIGNED,
        repo="YiAgent/openbot",
        actor="someone",
        issue_number=42,
        installation_id=1,
    )


def _tool(tools: list[Any], name: str) -> Any:
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"missing tool: {name}")


def test_make_fix_tools_returns_expected_set() -> None:
    sandbox = _StubSandbox()
    tools = make_fix_tools(sandbox=sandbox, event=_event())
    names = sorted(t.name for t in tools)
    assert names == [
        "git_diff",
        "list_files",
        "read_file",
        "run_command",
        "search_files",
        "write_file",
    ]


async def test_read_and_write_file_round_trip() -> None:
    sandbox = _StubSandbox(files={"src/app.py": "old"})
    tools = make_fix_tools(sandbox=sandbox, event=_event())
    read = _tool(tools, "read_file")
    write = _tool(tools, "write_file")

    assert await read.coroutine(path="src/app.py") == "old"
    await write.coroutine(path="src/app.py", content="new\n")
    assert sandbox.files["src/app.py"] == "new\n"


async def test_run_command_executes_via_sandbox() -> None:
    sandbox = _StubSandbox(
        run_result=ExecResult(
            stdout="3 passed", stderr="", exit_code=0, timed_out=False
        )
    )
    tools = make_fix_tools(sandbox=sandbox, event=_event())
    run_command = _tool(tools, "run_command")

    result = await run_command.coroutine(command=["pytest", "-q"], timeout_seconds=30)

    assert sandbox.run_calls == [["pytest", "-q"]]
    assert result["exit_code"] == 0
    assert result["stdout"] == "3 passed"
    assert result["timed_out"] is False


async def test_search_files_wraps_grep() -> None:
    sandbox = _StubSandbox(
        run_result=ExecResult(
            stdout="src/a.py:12:foo\nsrc/b.py:33:foo\n",
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    tools = make_fix_tools(sandbox=sandbox, event=_event())
    search = _tool(tools, "search_files")

    hits = await search.coroutine(pattern="foo", path_glob="src/**")

    assert hits == ["src/a.py:12:foo", "src/b.py:33:foo"]
    # First two argv positions are `grep -rn`; arbitrary glob handling
    # is the sandbox's job, not the tool's.
    assert sandbox.run_calls[0][:2] == ["grep", "-rn"]


async def test_tool_budget_drains_after_max_calls() -> None:
    sandbox = _StubSandbox()
    budget = ToolBudget(remaining=2)
    tools = make_fix_tools(sandbox=sandbox, event=_event(), budget=budget)
    read = _tool(tools, "read_file")

    await read.coroutine(path="a")
    await read.coroutine(path="b")
    with pytest.raises(ToolBudgetExceededError) as exc_info:
        await read.coroutine(path="c")
    assert exc_info.value.tool == "read_file"


def test_default_budget_constant_is_twenty() -> None:
    """Locked by spec — bumping this is a deliberate code change.
    See `docs/superpowers/specs/2026-05-20-fix-deepagent-design.md` §Responder."""
    assert DEFAULT_FIX_TOOL_BUDGET == 20


async def test_git_diff_returns_sandbox_diff() -> None:
    sandbox = _StubSandbox(diff_text="diff --git a/x b/x\n+hello\n")
    tools = make_fix_tools(sandbox=sandbox, event=_event())
    diff = _tool(tools, "git_diff")
    assert (await diff.coroutine()).startswith("diff --git")
```

- [ ] **Step 1.2: Verify the tests fail with `ModuleNotFoundError`.**

Run: `uv run pytest tests/infrastructure/agents/test_fix_tools.py -v`
Expected: 7 failures, each one
`ModuleNotFoundError: No module named 'openbot.infrastructure.agents._fix_tools'`.

---

### Step 2: Implement `_fix_tools.py`

- [ ] **Step 2.1: Create `openbot/infrastructure/agents/_fix_tools.py`.**

```python
"""LangChain tool wrappers for DeepAgentsFixResponder (slice C.6).

Six tools, all closing over a per-event ``SandboxPort`` + ``ToolBudget``.
The fix loop's tools are read/write inside the cloned sandbox; none of
them reach GitHub. PR creation and branch push are the use case's job,
not the agent's, so the agent stays confined to the workspace.

Mirrors slice A2's ``_review_tools.py`` shape. The only differences:
  - Higher default budget (20 vs 5) — fix runs need more headroom.
  - Includes write_file / run_command — review tools are read-only.
  - search_files uses sandbox-local grep, not GitHub Code Search,
    because we work inside a cloned tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.tools import StructuredTool

if TYPE_CHECKING:
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.domain.events import UnifiedEvent


# Tighter than it looks: read_file × 6 + write_file × 3 + run_command × 5
# + list_files × 2 + git_diff × 1 + search_files × 3 = 20 typical calls
# for a non-trivial fix. The agent gets one budget item beyond "typical"
# so a single retry is OK; a runaway loop trips immediately.
DEFAULT_FIX_TOOL_BUDGET = 20


class ToolBudgetExceededError(RuntimeError):
    """Raised when a tool call would exceed the per-run budget.

    DeepAgents surfaces this back to the model as a tool error message
    rather than crashing the agent — the model is expected to wrap up
    with whatever findings it has, not retry.
    """

    def __init__(self, tool: str) -> None:
        super().__init__(f"tool budget exhausted on call to {tool!r}")
        self.tool = tool


@dataclass
class ToolBudget:
    """Counts down across tool invocations within a single fix run."""

    remaining: int

    def consume(self, tool: str) -> None:
        if self.remaining <= 0:
            raise ToolBudgetExceededError(tool)
        self.remaining -= 1


def _exec_result_to_dict(result: Any) -> dict[str, Any]:
    """Surface ExecResult as a JSON-friendly dict — agents handle dicts better."""
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
    }


def make_fix_tools(
    *,
    sandbox: SandboxPort,
    event: UnifiedEvent,  # noqa: ARG001 — reserved for per-event logging
    budget: ToolBudget | None = None,
) -> list[StructuredTool]:
    """Build the per-run fix tool list.

    All tools close over ``(sandbox, budget)``. ``event`` is accepted but
    unused today — kept in the signature so future logging can include
    per-event identifiers without breaking callers.
    """
    bud = budget if budget is not None else ToolBudget(remaining=DEFAULT_FIX_TOOL_BUDGET)

    async def read_file(path: str) -> str:
        bud.consume("read_file")
        return await sandbox.read_file(path)

    async def write_file(path: str, content: str) -> str:
        bud.consume("write_file")
        await sandbox.write_file(path, content)
        return f"wrote {len(content)} bytes to {path}"

    async def list_files(path: str = ".", max: int = 200) -> list[str]:
        bud.consume("list_files")
        return await sandbox.list_files(path=path, max=max)

    async def run_command(
        command: list[str], timeout_seconds: int = 60
    ) -> dict[str, Any]:
        bud.consume("run_command")
        result = await sandbox.run(command=command, timeout_seconds=timeout_seconds)
        return _exec_result_to_dict(result)

    async def git_diff() -> str:
        bud.consume("git_diff")
        return await sandbox.git_diff()

    async def search_files(
        pattern: str, path_glob: str | None = None
    ) -> list[str]:
        bud.consume("search_files")
        cmd: list[str] = ["grep", "-rn", pattern]
        if path_glob:
            cmd.extend(["--include", path_glob])
        cmd.append(".")
        result = await sandbox.run(command=cmd, timeout_seconds=30)
        if result.exit_code not in (0, 1):  # grep exit 1 = no matches
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    return [
        StructuredTool.from_function(
            coroutine=read_file,
            name="read_file",
            description=(
                "Read a UTF-8 file from the sandbox workspace. Returns "
                "empty string if missing."
            ),
        ),
        StructuredTool.from_function(
            coroutine=write_file,
            name="write_file",
            description="Replace a file's contents in the sandbox workspace.",
        ),
        StructuredTool.from_function(
            coroutine=list_files,
            name="list_files",
            description="List files under `path` (default '.') up to `max` results.",
        ),
        StructuredTool.from_function(
            coroutine=run_command,
            name="run_command",
            description=(
                "Run an argv-list command in the workspace. Returns "
                "{stdout, stderr, exit_code, timed_out}. Use this to "
                "discover and execute the project's tests."
            ),
        ),
        StructuredTool.from_function(
            coroutine=git_diff,
            name="git_diff",
            description="Return the working-tree diff after your edits.",
        ),
        StructuredTool.from_function(
            coroutine=search_files,
            name="search_files",
            description=(
                "grep -rn for `pattern` across the workspace, optionally "
                "filtered by `path_glob` (a grep --include pattern). Returns "
                "lines formatted `path:line:fragment`."
            ),
        ),
    ]


__all__ = [
    "DEFAULT_FIX_TOOL_BUDGET",
    "ToolBudget",
    "ToolBudgetExceededError",
    "make_fix_tools",
]
```

- [ ] **Step 2.2: Run the 7 tests.**

Run: `uv run pytest tests/infrastructure/agents/test_fix_tools.py -v`
Expected: 7 passes.

---

### Step 3: Run `make check` and commit C.6

- [ ] **Step 3.1: Cross-suite check.**

Run: `make check`
Expected: all green. Slice B tests still pass; new tool tests added under
`tests/infrastructure/agents/`.

- [ ] **Step 3.2: Stage and commit.**

```bash
git add openbot/infrastructure/agents/_fix_tools.py \
        tests/infrastructure/agents/test_fix_tools.py
git commit -m "feat(agents): add make_fix_tools factory + ToolBudget (slice C.6)

Six per-event StructuredTools for DeepAgentsFixResponder, closing over
SandboxPort + a fix-specific ToolBudget (default 20 — higher than
review's 5 because fix runs need read_file × N + write_file × M +
run_command × K headroom).

Tools:
- read_file(path) -> str
- write_file(path, content) -> str (echoes byte count)
- list_files(path='.', max=200) -> list[str]
- run_command(command, timeout_seconds=60) -> {stdout, stderr, exit_code, timed_out}
- git_diff() -> str
- search_files(pattern, path_glob=None) -> list[str]  # sandbox-local grep

None touch GitHub — PR/branch creation is the use case's job (C.8), not
the agent's. search_files uses sandbox-local grep rather than GitHub
Code Search because we work inside a freshly cloned tree.

7 unit tests with a hand-rolled StubSandbox that satisfies SandboxPort.
DEFAULT_FIX_TOOL_BUDGET is frozen at 20 by an explicit assertion — bumps
require a deliberate spec amendment."
```

- [ ] **Step 3.3: Verify the commit.**

Run: `git log --oneline -1`
Expected: C.6 commit at HEAD.

---

## C.6 acceptance checks

- [ ] `make check` green after commit.
- [ ] All 6 fix-loop tools close over `(sandbox, budget)` — none take
      `adapter`. (Test imports verify this.)
- [ ] `DEFAULT_FIX_TOOL_BUDGET == 20` asserted in tests.
- [ ] `ToolBudgetExceededError.tool` attribute names the offending tool
      (assertion in test).
- [ ] No imports from `evals/` anywhere in `_fix_tools.py` or its tests.
- [ ] `lint-imports` passes.

---

## Heads-up for part 6 (C.7 responder + C.8 use case)

The responder built next will:

- Import `make_fix_tools` from `_fix_tools.py` (signature locked above).
- Import `FixOutcomeSchema` from C.2.
- Take `(event, adapter, sandbox, issue)` and return `FixOutcome`.
- Use `model="anthropic:claude-opus-4-7"` (PRD §13 #2 lock — already
  threaded through `Feature.FIX` in the model router).

The use case wiring after that consumes the new
`PreflightContext.sandbox_factory` (added in C.8) plus all four channel
adapter methods from C.4. If any signature drift surfaces, fix the
earlier task before continuing.
