# Review / Fix DeepAgent integration — slice plan

**Status:** slices A + A2 + B landed. Slice C deferred.
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

## Slice A2 (DONE — tool-using reviewer)

### What landed

- `ChannelAdapterPort.read_file(event, path) -> str`
  - Returns UTF-8 text or `""` on missing/binary; tools don't branch on failure.
  - `GitHubAdapter.read_file` delegates to `fetch_repo_file` for the bytes-level fetch and decodes.
- `ChannelAdapterPort.grep_repo(event, *, pattern, path_glob, max_matches=20) -> list[str]`
  - Backed by GitHub Code Search REST (`/search/code` with text-match Accept header).
  - Returns `[]` on 422 (unindexed/malformed) so transient backend issues don't block review.
  - Hits formatted `"{path}: {fragment}"` (first line of first text-match).
- `openbot/infrastructure/agents/_review_tools.py` — `make_review_tools(adapter, event)` builds a per-event `[read_file, grep_repo]` list of `StructuredTool`s.
- `ToolBudget` enforces `DEFAULT_TOOL_BUDGET = 5` total invocations per review run; the 6th call raises `ToolBudgetExceededError` (surfaced as a tool error to the agent so it synthesizes a final answer).
- `DeepAgentsReviewResponder`:
  - Removed `@lru_cache(_agent_for_model)` — tools close over `(adapter, event)`, so caching by model alone was a multi-tenant correctness bug, not just an optimization miss.
  - Builds a fresh agent per `review_for_event` call. Cheap relative to the LLM call.
  - Passes `config={"recursion_limit": 25}` on `ainvoke` to catch non-tool loops the budget guard can't see.
  - System prompt teaches when to use tools vs trust the inline diff.

### Tests

- 8 new adapter tests (3× `read_file`, 5× `grep_repo`) — all via `httpx.MockTransport`, no network.
- 7 `_review_tools.py` tests — tool surface, budget enforcement, per-call freshness.
- 8 responder tests (2 added: `test_review_responder_rebuilds_agent_per_event`, `test_review_responder_passes_recursion_limit`).
- `FakeChannelAdapter` + `RecordingGitHubAdapter` got `read_file` / `grep_repo` stubs so the contract still holds.
- 855 → 878 passing.

### Out-of-scope for slice A2

- Smarter tool selection (e.g. "AST-walk this function" vs raw `read_file`) — slice B.
- Caching `read_file` results within a single review run — current call counts are low enough that an LRU on the tool wrapper would be premature.
- Surfacing tool budget exhaustion in the PR reply — agent currently absorbs it via the final-answer path. If reviewers report "agent gave up early", revisit.

---

## Slice B (DONE — structured findings + PR Review API)

### Locked design decisions (from session 2026-05-20)

| Q | Decision | Rationale |
|---|----------|-----------|
| Verdict ceiling | **COMMENT-only** — never `REQUEST_CHANGES` regardless of severity | OpenBot is advisory in v0.1 (PRD §13). Humans still make merge calls. Lowers blast radius if the model gets things wrong. |
| Zero findings | **APPROVE review with summary** | Posting a real APPROVE makes "no findings above threshold" feel intentional rather than a no-op. |

### What landed

- `openbot/domain/review.py`
  - Frozen `Finding(severity, file, message, line=None, quote=None)`, frozen `ReviewFindings(summary, findings=())`.
  - `Severity = Literal["critical", "high", "medium", "low", "nit"]` — strict superset of `SeverityThreshold` (which omits `nit`; threshold of "nit" would be useless).
  - `passes_threshold(finding, threshold)` ranks by tuple index; unknown severities silently drop rather than crash the run (LLM hallucination resilience).
- `openbot/infrastructure/agents/_review_schema.py`
  - Pydantic `_FindingModel` / `_ReviewFindingsModel` with `extra="forbid"` — schema drift is a deliberate code change.
  - `to_domain()` is the anti-corruption boundary; pydantic does not leak past this module.
  - `parse_structured_response()` accepts pydantic instance OR plain dict; raises on anything else so silent approve on garbage is impossible.
- `ChannelAdapterPort.create_pr_review(event, pr_number, *, body, event_type, comments)`
  - `event_type: Literal["APPROVE", "COMMENT"]` — `REQUEST_CHANGES` is unreachable by construction.
  - `GitHubAdapter` impl POSTs to `/repos/{owner}/{repo}/pulls/{n}/reviews` via the existing `_authed_json` helper.
- `DeepAgentsReviewResponder`
  - Returns `ReviewFindings` (was `str`).
  - Passes `response_format=ReviewFindingsSchema` to `create_deep_agent`.
  - Reads `result["structured_response"]`; fails loud if missing.
  - System prompt rewritten to demand structured output and explain when to omit `line` (repo-wide findings).
- `maybe_run_review` (use case)
  - Calls the responder, filters by `ctx.config.severity_threshold`, partitions into inline (`line is not None`) and repo-wide.
  - Inline findings → PR review comments shaped `{path, line, body: "**severity** — message"}` (quote rendered as a fenced block when present).
  - Repo-wide findings → folded into the review body under "Repo-wide notes" (PR Review API rejects line-less inline comments).
  - Verdict: `APPROVE` iff filtered findings is empty, else `COMMENT`. PRD-locked, asserted by `test_never_emits_request_changes_even_on_critical`.
  - Responder failure → fallback COMMENT review with the error template (so the PR author still sees a real signal, never silent).

### Tests

- `tests/domain/test_review.py` — 16 tests covering immutability, defaults, and the full severity-ranking matrix (including unknown severities).
- `tests/infrastructure/agents/test_review_schema.py` — 7 tests covering `to_domain()` happy/drop paths, `parse_structured_response` shape acceptance, and `extra="forbid"`.
- `tests/infrastructure/adapters/test_github.py` — 3 new tests for `create_pr_review` (APPROVE body-only, COMMENT with inline comments, missing-auth guard).
- `tests/infrastructure/agents/test_deepagents_review.py` — rewritten for structured output (8 tests; added `test_review_responder_returns_findings_from_structured_response` and `test_review_responder_raises_on_missing_structured_response`).
- `tests/application/use_cases/test_review.py` — 9 new tests covering approve, comment with inline comments, severity filter (drop and keep paths), repo-wide folding, responder failure, both skip-conditions, and the no-REQUEST_CHANGES lock.
- `tests/e2e/conftest.py` — `_fake_review_findings` returns a `ReviewFindings`; `RecordingGitHubAdapter` records via new `pr_reviews` list.
- `tests/e2e/test_spec_demos.py` — demo 02 asserts on `pr_reviews` (APPROVE shape); demo 07 split between `replies` (announce) and `pr_reviews` (review).
- 878 → 915 passing.

### Out-of-scope for slice B

- Re-review on `PR_SYNCHRONIZED` — incremental review wiring lives in F3 (already in router); the responder is event-stateless by design. A future "diff since last review" feature is a separate slice.
- Suggested edits (`suggestion` code blocks) — GitHub renders them as one-click commits. Useful but the agent would need to emit a target line range, not just a single line. Defer until reviewers ask for it.
- LangSmith feedback on individual findings — would require persisting `findings_id` per comment. Out of scope; the chain-level trace is enough for v0.1.

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

## Acceptance checks (slice A2)

- [x] `make check` passes (fmt + lint + 878 tests).
- [x] `FakeChannelAdapter` + `RecordingGitHubAdapter` still satisfy `ChannelAdapterPort`.
- [x] Tool budget exhaustion does not crash the responder (raises `ToolBudgetExceededError` to the agent; agent absorbs as tool-error path).
- [x] No new prompt-quality assertions in `tests/`.
- [x] No new network calls in unit tests.
- [ ] Smoke test on a real PR — does the agent actually fetch files when the diff is insufficient? (manual, after merge.)
- [ ] Cost test — how many tool calls does opus-4-7 average across a small batch of real PRs? Confirm `DEFAULT_TOOL_BUDGET = 5` is enough headroom.

## Acceptance checks (slice B)

- [x] `make check` passes (fmt + lint + 915 tests).
- [x] `FakeChannelAdapter` + `RecordingGitHubAdapter` still satisfy `ChannelAdapterPort` (now including `create_pr_review`).
- [x] Domain layer has no pydantic dependency — verified by the hexagonal contract (import-linter green).
- [x] `event_type` is `Literal["APPROVE", "COMMENT"]` — `REQUEST_CHANGES` is unreachable by construction (PRD §13 lock).
- [x] LLM hallucinations on `severity` are dropped, not raised (`test_to_domain_drops_unknown_severity` + `passes_threshold` test for unknown).
- [x] No new prompt-quality assertions in `tests/`.
- [x] No new network calls in unit tests.
- [ ] Smoke test on a real PR — does the structured-output pass actually produce parseable findings on opus-4-7? (manual, after merge.)
- [ ] Configure `.openbot/config.yaml` `review.severity_threshold` per repo and verify filter behavior (manual.)

---

## Files touched

### Slice A
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

### Slice A2
```
openbot/application/ports/channel_adapter.py        +30  (read_file + grep_repo on port)
openbot/infrastructure/adapters/github.py           +90  (read_file + grep_repo impl)
openbot/infrastructure/agents/_review_tools.py      +130 (new — tool factory + ToolBudget)
openbot/infrastructure/agents/deepagents_review.py  ~50  (per-event build, tools wired, prompt updated)
tests/_fakes/channel_adapter.py                     +14  (port conformance)
tests/e2e/conftest.py                               +22  (RecordingGitHubAdapter parity)
tests/infrastructure/adapters/test_github.py        +130 (8 tests)
tests/infrastructure/agents/test_deepagents_review.py +60 (2 new tests + ainvoke signature updates)
tests/infrastructure/agents/test_review_tools.py    +130 (new file, 7 tests)
```

### Slice B
```
openbot/domain/review.py                             +85  (new — Finding/ReviewFindings + passes_threshold)
openbot/application/ports/channel_adapter.py        +25  (create_pr_review)
openbot/application/use_cases/review.py             ~120 (severity filter + PR Review API submission)
openbot/infrastructure/adapters/github.py           +25  (create_pr_review impl)
openbot/infrastructure/agents/_review_schema.py     +120 (new — pydantic ⇄ domain boundary)
openbot/infrastructure/agents/deepagents_review.py  ~40  (structured response_format + prompt)
tests/_fakes/channel_adapter.py                     +20  (port conformance)
tests/e2e/conftest.py                               +25  (RecordingGitHubAdapter parity + monkeypatch swap)
tests/e2e/test_spec_demos.py                        ~20  (demo 02 + demo 07 reshape for PR Review API)
tests/domain/test_review.py                         +80  (new — 16 tests)
tests/application/use_cases/test_review.py          +220 (new — 9 tests)
tests/infrastructure/adapters/test_github.py        +60  (3 tests)
tests/infrastructure/agents/test_review_schema.py   +85  (new — 7 tests)
tests/infrastructure/agents/test_deepagents_review.py ~50 (rewritten for structured_response path)
```
