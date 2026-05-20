# Review / Fix DeepAgent integration — slice plan

**Status:** slice A landed. Slice B/C deferred.
**Branch:** `feat/review-deepagent`
**PRD anchors:** §4.2 (review), §4.3 (fix), §13 #2 (locked model routing)

---

## Locked decisions (from session 2026-05-20)

| Q | Decision | Rationale |
|---|----------|-----------|
| Q1 (tools) | **Minimal — inline diff, `tools=[]`** | `ChannelAdapterPort` exposes neither `read_file` nor `grep_repo` yet; widening the port to add them would blow up slice A. `get_pr_diff` is the only port change we ship this slice. |
| Q2 (Fix workflow) | **Defer to v0.2** | Fix touches a sandbox (`SandboxPort`) + PR creation flow; out of scope for "make review do something". |
| Q3 (TaskSpec) | **Don't widen TaskSpec for slice A** | The responder reads the diff via `ctx.adapter.get_pr_diff` at handler time; no need to thread it through the queue. Revisit when slice B introduces structured output. |
| Q4 (tracing) | **`@_traceable(run_type="chain", name="review")`** | Matches chat / triage / fix — one chain span per webhook event. |

## Slice A (this commit, DONE)

### What landed

- `ChannelAdapterPort.get_pr_diff(event, pr_number) -> str`
  - Returns the unified diff via `Accept: application/vnd.github.v3.diff`.
  - Returns `""` on 404 (closed / deleted PRs must not block review).
  - Raises on other HTTP errors (transient gateway failures bubble to caller).
- `GitHubAdapter.get_pr_diff` — bypasses `_authed_json` (which assumes JSON body); manages its own request + headers.
- `DeepAgentsReviewResponder` (mirrors `DeepAgentsChatResponder`):
  - `tools=[]`, opus-4-7 model, system prompt = senior code reviewer.
  - Diff is truncated to `_MAX_DIFF_CHARS = 64_000` (~16k tokens) before prompting.
  - Empty diff → "(diff unavailable…)" placeholder + the agent answers with a brief no-op note.
- `maybe_run_review`:
  - LLM call happens **outside** the `audit_lifecycle` span (slow LLM ≠ open STARTED row for minutes).
  - On responder failure → `_ERROR_TEMPLATE` posted, so the PR author sees real feedback instead of silence.
  - On reply failure → audit row marked FAILED, exception swallowed (GitHub already got 202).

### Tests (TDD-first)

- `tests/infrastructure/adapters/test_github.py` — 3 tests for `get_pr_diff` (200, 404, 5xx).
- `tests/infrastructure/agents/test_deepagents_review.py` — 6 tests covering model, prompt shape, empty diff, truncation, block-content extraction, empty-reply guard, missing-pr-number guard.
- `tests/_fakes/channel_adapter.py` — added `get_pr_diff` stub returning `""` to keep `FakeChannelAdapter` conforming to the port.

### Out-of-scope for slice A

- Structured findings (severity, file, line, rule_id).
- Multi-turn conversation / "ask for more context".
- Re-reviewing the same PR after `synchronize` (incremental review wiring lives in F3 — already shipped, just doesn't have a real reviewer behind it until slice B).
- Tool-using agent (read_file, grep_repo, list_files).

---

## Slice A2 (next — tools without changing the contract)

Goal: turn the inline-diff agent into a tool-using agent **without** breaking slice A's tests.

Steps:

1. Add `read_file(path) -> str` and `grep_repo(pattern, path_glob) -> list[str]` to `ChannelAdapterPort`. Implement on `GitHubAdapter` via the Contents API + (probably) a simple grep over fetched files. Keep `fetch_repo_file` as the bytes-level primitive.
2. Wrap each port method as a LangChain `@tool` in `openbot/infrastructure/agents/_review_tools.py`. Tools close over the adapter + event.
3. Rebuild the responder agent with `tools=[read_file_tool, grep_repo_tool]` per-event (can't cache by model anymore since tools close over event).
4. Update system prompt to teach the agent when to fetch files vs trust the diff.

Risks:
- Token budget — opus-4-7 can chew through a lot of context if the agent grep-spams. Add a per-call tool-invocation cap (e.g., 5 tool calls / run).
- Cache strategy needs revisiting — `_agent_for_model(model)` won't work once tools bind to event state.

---

## Slice B (structured findings)

Goal: emit one comment per high-confidence finding instead of one consolidated reply.

- Add a Pydantic schema for findings (`severity`, `file`, `line`, `message`, `quote`).
- Switch responder to `with_structured_output` or a final-pass extractor.
- Adapter gets `add_pr_review_comments(event, findings)` — uses the PR review API (`POST /repos/.../pulls/{n}/reviews` with `comments` array, single REQUEST_CHANGES/COMMENT review).
- Severity filter from `.openbot/config.yaml` `review.min_severity`.

## Slice C (Fix workflow)

Goal: implement `openbot/application/use_cases/fix.py` end-to-end.

- Needs sandbox (`SandboxPort`) — already pluggable via `evals.sandboxes.factory`, just needs wiring into the application layer.
- Needs PR-creation permission on the GitHub App.
- Needs a separate responder (`DeepAgentsFixResponder`) with tools = `read_file`, `write_file`, `run_tests`, `create_branch`, `open_pull_request`.
- Defer until slice A2 + B are stable so we have a working reviewer to grade fix output against.

---

## Acceptance checks (slice A)

- [x] `make test` passes (responder + adapter tests).
- [x] No new lint warnings (`make lint`).
- [x] `FakeChannelAdapter` still satisfies `ChannelAdapterPort` (contract test).
- [x] No tests assert prompt-quality / LLM-behavior (those belong in `evals/`, per CLAUDE.md).
- [x] No new network calls in unit tests (everything mocked via `httpx.MockTransport` + monkeypatched `create_deep_agent`).
- [ ] Smoke test against a real PR — manual, after merge.

---

## Files touched

```
openbot/application/ports/channel_adapter.py        +9   (port method)
openbot/application/use_cases/review.py             ~50  (wired responder, error fallback)
openbot/infrastructure/adapters/github.py           +20  (get_pr_diff impl)
openbot/infrastructure/agents/__init__.py           +2   (export)
openbot/infrastructure/agents/deepagents_review.py  +147 (new file)
tests/_fakes/channel_adapter.py                     +3   (port conformance)
tests/infrastructure/adapters/test_github.py        +40  (3 tests + sample diff)
tests/infrastructure/agents/test_deepagents_review.py +170 (new file, 6 tests)
```
