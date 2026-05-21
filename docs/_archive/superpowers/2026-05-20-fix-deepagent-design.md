# Slice C — Fix workflow end-to-end (design)

**Status:** design. Awaiting implementation plan.
**Branch:** `feat/review-deepagent` (continues from slice B).
**PRD anchors:** §4.3 (fix), §13 #2 (locked model routing), §3 (locked boundaries — sandbox).

---

## Goal

Replace the stub `maybe_run_fix` (currently just an ACK comment) with a real
end-to-end fix loop:

1. Webhook arrives (issue assigned to bot, *or* labeled `openbot:fix`).
2. Spin up a sandbox, clone the repo at default-branch HEAD.
3. DeepAgent reads the issue, edits files, picks and runs a test command.
4. If tests pass: push to `openbot/fix-issue-{n}-{short_sha}`, open a PR,
   reply on the issue with the PR link.
5. If tests fail: reply on the issue with the attempt summary + test output.
   No PR.
6. Either way, sandbox is destroyed before the handler exits.

The slice is intentionally conservative on side effects — one attempt per
webhook, no draft PRs, no automatic merge.

---

## Locked decisions

| Topic | Decision | Rationale |
|---|---|---|
| Scope | **Full end-to-end** (sandbox + adapters + responder + use case). | Single coherent slice; partial landings (sandbox-only or responder-only) would leave dead code that can't be exercised. |
| Sandbox backend | **Daytona** (PRD §3 default). | Already the eval default. Long-lived workspace semantics match a per-PR fix attempt. |
| Trigger | **`ISSUE_ASSIGNED` OR `ISSUE_LABELED('openbot:fix')`**. | Assignment matches Copilot Workspace UX; label gives maintainers an explicit go-button. Both already exist in the router. |
| Loop policy | **Single attempt. PR iff tests pass; otherwise comment.** | Advisory-only stance from slice B. Don't open speculative PRs. Iteration is the maintainer's call. |
| Test command | **Agent decides via `run_command` tool.** | Repo ecosystems vary too much for static detection. Sandbox isolation contains blast radius. |
| Verdict ceiling | **Never `REQUEST_CHANGES`, never auto-merge.** | Carries forward slice B's PRD §13 lock. |

---

## Architecture

### Layer map (hexagonal contract preserved)

```
domain/
  fix.py                            # NEW: frozen FixAttempt, FixOutcome

application/
  ports/
    sandbox.py                      # GROWS: clone/read_file/write_file/list_files/run/git_diff/commit_and_push/close
    channel_adapter.py              # GROWS: get_issue, create_branch, open_pull_request
  use_cases/
    fix.py                          # REWRITTEN: real orchestration
  middleware/
    preflight.py                    # GROWS: PreflightContext gains sandbox_factory

infrastructure/
  sandboxes/                        # NEW package
    fake.py                         # FakeSandboxAdapter — local tmpdir, for tests + dev
    daytona.py                      # DaytonaSandboxAdapter — production impl
  agents/
    deepagents_fix.py               # NEW: DeepAgentsFixResponder
    _fix_tools.py                   # NEW: per-event [clone_workspace, read_file, write_file, run_command, list_files, search_files]
    _fix_schema.py                  # NEW: pydantic ⇄ domain (FixOutcomeSchema)
  adapters/
    github.py                       # GROWS: get_issue, create_branch, open_pull_request impls
```

### Per-event flow

```
ISSUE_ASSIGNED / ISSUE_LABELED webhook
        ↓
Router → maybe_run_fix(ctx)
        ↓
PreflightContext { adapter, sandbox_factory, ... }
        ↓
issue = await ctx.adapter.get_issue(event, event.issue_number)
        ↓
async with ctx.sandbox_factory() as sandbox:
    await sandbox.clone(repo_url=issue["clone_url"],
                        ref=issue["default_branch"],
                        token=<installation_token>)

    outcome: FixOutcome = await responder.fix_for_event(
        event,
        adapter=adapter,
        sandbox=sandbox,
        issue=issue,
    )

    if outcome.attempt.tests_passed:
        short_sha = issue["base_sha"][:7]
        branch_ref = f"openbot/fix-issue-{event.issue_number}-{short_sha}"
        token = await adapter.get_installation_token(event)
        await adapter.create_branch(event, branch_ref, from_sha=issue["base_sha"])
        await sandbox.commit_and_push(branch_ref=branch_ref,
                                       message=outcome.attempt.summary,
                                       token=token)
        pr = await adapter.open_pull_request(event,
                                              title=f"Fix #{n}: {summary}",
                                              body=<PR body with closes #n>,
                                              head=branch_ref,
                                              base=issue["default_branch"])
        await adapter.reply(event, f":robot: opened {pr['html_url']}")
    else:
        await adapter.reply(event, <tests-failed template with output>)
```

### Per-event sandbox lifetime

One sandbox per webhook handler invocation. Created on entry, destroyed
on exit (success, failure, or cancellation). No reuse across events.
Same rationale as A2's per-event tool list: multi-tenant safety > a
marginal cold-start savings.

---

## Data model

```python
# openbot/domain/fix.py
@dataclass(frozen=True, slots=True)
class FixAttempt:
    """One reasoning pass through the fix loop.

    Holds everything the use case needs to decide between PR vs comment.
    """
    summary: str                            # one-line description
    files_changed: tuple[str, ...]          # paths the agent wrote
    tests_passed: bool                      # final test exit_code == 0
    test_command: str                       # command the agent chose
    test_output: str                        # stdout+stderr, truncated
    diff: str                               # git diff of working tree

@dataclass(frozen=True, slots=True)
class FixOutcome:
    """End-to-end result of maybe_run_fix.

    `pr_url` is set only when tests_passed and a PR was opened.
    `error` is set only when something raised before completing.
    """
    attempt: FixAttempt
    pr_url: str | None = None
    error: str | None = None
```

`FixOutcome` is intentionally permissive on `(pr_url, error)` — the use
case decides which one to set. Invariants asserted via tests, not
dataclass `__post_init__`, because partial outcomes do exist (e.g.,
tests passed but `open_pull_request` failed → `pr_url=None, error=...`).

---

## Port deltas

### `SandboxPort` (existing — grows)

```python
@dataclass(frozen=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool

class SandboxPort(Protocol):
    workspace: str  # absolute path inside the sandbox

    async def clone(self, *, repo_url: str, ref: str, token: str) -> None: ...
    async def read_file(self, path: str) -> str: ...
    async def write_file(self, path: str, content: str) -> None: ...
    async def list_files(self, *, path: str = ".", max: int = 200) -> list[str]: ...
    async def run(self, *, command: list[str], timeout_seconds: int = 60,
                  env: Mapping[str, str] | None = None) -> ExecResult: ...
    async def git_diff(self) -> str: ...
    async def commit_and_push(self, *, branch_ref: str, message: str, token: str) -> None: ...
    async def close(self) -> None: ...
```

Existing `run(command, env, timeout_seconds)` signature is preserved
(return type changes from `dict` → `ExecResult`). Callers in the eval
layer continue to use `evals.sandboxes.factory.SandboxBackend`, which is
**not** the same protocol — production fix loop and eval suites stay
separate per the port docstring at `openbot/application/ports/sandbox.py:7`.

### `ChannelAdapterPort` additions

```python
async def get_issue(
    self,
    event: UnifiedEvent,
    issue_number: int,
) -> dict[str, Any]:
    """Single REST batch — returns:
       {
         "title": str,
         "body": str,
         "comments": [{"author": str, "body": str}],
         "base_sha": str,           # default-branch HEAD
         "default_branch": str,
         "clone_url": str,
       }
    """

async def create_branch(
    self,
    event: UnifiedEvent,
    branch_ref: str,
    from_sha: str,
) -> None:
    """POST /repos/{owner}/{repo}/git/refs. 422 means the branch
    already exists — caller surfaces a tailored comment."""

async def open_pull_request(
    self,
    event: UnifiedEvent,
    *,
    title: str,
    body: str,
    head: str,
    base: str,
) -> dict[str, Any]:
    """POST /repos/{owner}/{repo}/pulls. Always draft=False — the
    fix loop never opens speculative PRs."""

async def get_installation_token(
    self,
    event: UnifiedEvent,
) -> str:
    """Return a short-lived installation access token for pushing.
    The existing `_authed_json` resolves this internally for REST
    calls; the use case needs the raw string to inject into the
    `https://x-access-token:{token}@github.com/...` URL the sandbox
    uses when pushing the fix branch. Cached internally by the
    adapter; the use case treats it as opaque and single-use."""
```

### `PreflightContext`

Add one field:

```python
@dataclass(frozen=True)
class PreflightContext:
    ...existing...
    sandbox_factory: Callable[[], AsyncContextManager[SandboxPort]] | None = None
```

Optional so review/triage/chat dispatches that don't need a sandbox
don't pay the cost. `maybe_run_fix` raises if `sandbox_factory is None`
when it actually needs one.

---

## Responder

`DeepAgentsFixResponder` mirrors `DeepAgentsReviewResponder`:

- Per-event build (no `lru_cache` — tools close over `(sandbox, adapter, event)`).
- Model: `anthropic:claude-opus-4-7` (PRD §13 #2 lock).
- `response_format=FixOutcomeSchema`.
- `config={"recursion_limit": 25}` carried over from review.
- `ToolBudget` capped at **20 invocations** (higher than review's 5 — fix
  needs read_file × N + write_file × M + run_command × K, easily 10+).
- System prompt: "you are a senior engineer; read the issue, propose a
  small focused fix, edit files, run the project's tests, return a
  structured outcome."

### Tools (per-event `StructuredTool` list)

| Tool | Purpose |
|---|---|
| `read_file(path)` | UTF-8 file contents, or "" on missing/binary. |
| `write_file(path, content)` | Replace file contents in the sandbox workspace. |
| `list_files(path=".", max=200)` | Recursive listing for orientation. |
| `search_files(pattern, path_glob)` | Substring search across the workspace. |
| `run_command(command, timeout_seconds=60)` | Arbitrary shell command in the workspace. The agent uses this to discover and run tests. |
| `git_diff()` | Working-tree diff after edits. |

All tools close over `(sandbox, event)`. None touch GitHub directly —
PR/branch creation is the use case's job, not the agent's.

---

## Error handling

| Failure | Response |
|---|---|
| `event.issue_number is None` / `installation_id is None` | Skip + log; no comment |
| `adapter.get_issue` 404 | Skip + log; no comment |
| `sandbox.clone` raises | Comment: "couldn't clone repo — check bot's repo access" |
| Responder raises | Comment: error template (same shape as review's) |
| `outcome.attempt.tests_passed == False` | Comment: tried but tests failed; include `test_command` + truncated `test_output` |
| `adapter.create_branch` 422 | Comment: "there's already an open fix attempt — close that PR first" |
| `sandbox.commit_and_push` raises | Comment: "couldn't push the branch — likely a permissions issue" |
| `adapter.open_pull_request` raises | Comment: "made the changes but couldn't open the PR — branch is at `<ref>`" |
| `sandbox.close` raises | Log only; never user-visible |

The whole pipeline runs inside `async with sandbox_factory() as sandbox:`,
so cleanup is guaranteed. Per-step try/except blocks let us post a
tailored comment instead of one generic "something broke" message.

---

## Tests (target ~71 new; 915 → ~986)

| Layer | File | Count |
|---|---|---|
| Domain | `tests/domain/test_fix.py` | 8 |
| Schema | `tests/infrastructure/agents/test_fix_schema.py` | 6 |
| Fake sandbox | `tests/infrastructure/sandboxes/test_fake.py` | 12 |
| Daytona adapter | `tests/infrastructure/sandboxes/test_daytona.py` | 6 (mock Daytona SDK) |
| Fix tools | `tests/infrastructure/agents/test_fix_tools.py` | 7 |
| GitHub adapter | `tests/infrastructure/adapters/test_github.py` | +8 (3× `get_issue`, 2× `create_branch`, 2× `open_pull_request`, 1× `get_installation_token`) |
| Responder | `tests/infrastructure/agents/test_deepagents_fix.py` | 8 |
| Use case | `tests/application/use_cases/test_fix.py` | 14 (happy path; tests-fail; clone-fail; agent-fail; branch-conflict; push-fail; open-pr-fail; skip cases) |
| E2E demo | `tests/e2e/test_spec_demos.py` | +1 (demo 08 — fix happy path posts PR link reply) |

No tests assert prompt-quality or LLM-behavior — those live in `evals/`
per CLAUDE.md. Adapter tests use `httpx.MockTransport`; responder tests
monkeypatch `create_deep_agent`; sandbox tests use the in-process
`FakeSandboxAdapter`.

---

## Implementation order (sub-commits within the slice)

To keep each commit reviewable, the implementation plan should land in
this order. Each step ends green (`make check` passes).

1. **C.1 — Domain**: `openbot/domain/fix.py` + tests.
2. **C.2 — Schema**: `openbot/infrastructure/agents/_fix_schema.py` + tests.
3. **C.3 — Sandbox port + fake adapter**: grow `SandboxPort`, add
   `FakeSandboxAdapter`, update `FakeChannelAdapter` / `RecordingGitHubAdapter`
   if needed. Daytona impl is a stub at this step (just so import works);
   real impl lands in C.5.
4. **C.4 — Channel adapter additions**: `get_issue`, `create_branch`,
   `open_pull_request` on port + `GitHubAdapter` + tests.
5. **C.5 — Daytona sandbox adapter**: real impl behind a mocked Daytona SDK;
   integration verified via `make check`.
6. **C.6 — Fix tools**: `_fix_tools.py` + tests.
7. **C.7 — Responder**: `deepagents_fix.py` + tests.
8. **C.8 — Use case + PreflightContext wiring**: rewrite `maybe_run_fix`;
   add `sandbox_factory` to `PreflightContext`; thread it through DI.
9. **C.9 — E2E demo 08**: `test_spec_demos.py` demo for the fix happy path;
   `make check` final green; commit.

Each step gates the next via `make check`. Slice C ships as a single PR
when all steps land green.

---

## Out-of-scope (explicit)

- Multi-attempt self-fix loops.
- Auto-merge after CI green.
- Draft PRs.
- Re-running on `PR_SYNCHRONIZED` after maintainer pushes.
- Slash-command triggering (`/openbot fix`).
- Modal / Docker sandbox adapters (Daytona only for v0.1).
- Fix-against-PR (only fix-against-issue is in scope).
- LangSmith feedback on individual fix attempts.

---

## Acceptance checks

- [ ] `make check` passes (fmt + lint + import-linter + tests).
- [ ] Hexagonal contract green (`openbot/` does not import `evals/`).
- [ ] Domain layer has no pydantic dependency (verified by import-linter).
- [ ] `FakeChannelAdapter` + `FakeSandboxAdapter` still satisfy their ports.
- [ ] `RecordingGitHubAdapter` records `pr_creates`, `branch_creates`, `issue_lookups`.
- [ ] `event_type` from review unchanged; fix path uses `reply`, not `create_pr_review`.
- [ ] No new prompt-quality assertions in `tests/`.
- [ ] No new network calls in unit tests.
- [ ] Test count ≥ 986 (≥ 71 new).
- [ ] Smoke test on a real issue — manual, after merge. Did the agent
      actually push a branch and open a PR?
- [ ] Cost test — average tool calls + tokens for a small batch of real
      issues. Confirm `ToolBudget = 20` is enough headroom.
