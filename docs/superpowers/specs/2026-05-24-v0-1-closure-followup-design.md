# v0.1 Closure Follow-up — Outstanding gaps after 2026-05-22 closure

**Status:** design. Awaiting implementation plan.
**Date:** 2026-05-24
**Branch (proposed):** `feat/v0-1-closure-followup`
**Supersedes:** `_archive/superpowers/2026-05-22-v0-1-product-closure-design.md` (queue / sandbox / docs items only — see "Already closed" below).
**PRD anchors:** §1 (four core workflows), §4.1 (triage), §4.4 (chat), §4.5 (cost caps), §4.7 (cancellation), §4.8 (security egress), §8.2 (eval cutline).
**Related specs:**
  `_archive/superpowers/2026-05-22-v0-1-product-closure-design.md`,
  `_archive/superpowers/2026-05-22-deepagents-runtime-design.md`,
  `_archive/superpowers/2026-05-21-unified-sandbox-entry-design.md`.

---

## Goal

Land the four workstreams the prior closure spec scoped but did not finish, so v0.1
alpha readiness §1.2 is honestly satisfiable:

1. **Triage** picks up labels + priority on `main` (the work exists in commit
   `100a23a` on a stray remote branch and never merged).
2. **Chat** is repo-grounded read-only — `build_tools()` returns the actual
   `read_file` / `grep_repo` / `list_files` set, not `[]`.
3. **Bot-authored output** passes through one egress safety function before
   reaching `ChannelAdapter.reply` / `create_pr_review` / `create_pr`.
4. **Per-task agent-loop budget** stops a runaway agent before the next
   LLM/tool step, on top of the existing preflight budget gate.

This spec is a **gap-closing document**. It does not redesign anything; it
re-points the four open closure items at small, commit-sized changes.

---

## Already closed (do not re-do)

These items from the prior closure spec are verified done and out of scope here:

| Item | Evidence |
|---|---|
| Queue contract = TaskSpec v3 (webapp side) | `openbot/application/use_cases/ingest_webhook.py:317-327` builds a `TaskSpec` and calls `enqueue_task_spec(spec)`; commit `fd24eb2`. |
| Worker sandbox factory injection | `86a1c2c feat(worker): wire sandbox_factory through consume_loop → execute_handler` + `d6f7741 feat(sandbox): add build_sandbox_factory composition-root helper`. |
| Stale-test migration (no `run_dispatch` imports) | `tests/infrastructure/queue/test_worker_v3.py:38` reverse-asserts execute-handler usage; remaining `run_dispatch` mentions are docstrings only. |
| Docs alignment on sandbox + model | `0690414 chore: unify LLM config on ANTHROPIC_* prefix, route all features to GLM-5.1`; PRD/README updated. |

If a follow-up question lives in any of those rows, file an issue against the
prior spec, not this one.

---

## Outstanding gaps

### 1. Triage label + priority not on `main`

**Current state:**

`openbot/application/use_cases/triage.py` is the ACK-only version (96 lines,
`_ACK_TEMPLATE` literally says *"Auto-label, priority, and sandbox reproduce
land in upcoming commits"*).

The work to read `ctx.classifier_output: TriageClassifierOutput` and call
`adapter.add_label(...)` already exists as commit `100a23a`. That commit lives
on `origin/claude/compassionate-galileo-70CBi` and was never merged to `main`.

**Required v0.1 alpha behavior:**

1. On `ISSUE_OPENED` / `ISSUE_EDITED` / `ISSUE_REOPENED`, read the classifier
   output and apply at most one **type** label (`bug` / `enhancement` /
   `question` / `spam`) and one **priority** label (`priority:critical` /
   `priority:high` / `priority:medium` / `priority:low`).
2. Label application is best-effort: ACK comment is the primary user signal,
   label failure logs but does not raise.
3. The ACK comment includes the classification verdict when classifier output
   is present.
4. Bot-authored issues stay skipped (echo-loop defense).

**Locked decision:**

| Topic | Decision |
|---|---|
| Source of truth | `100a23a` is the reference implementation; cherry-pick or rewrite, but do not start from scratch. |
| Reproduce path | Sandbox-backed reproduce stays **deferred** — not in v0.1 alpha cutline. The PRD §4.1 "reproduce" sentence in `triage.py:15-16` becomes a TODO comment pointing to `v0.2`, not a planned slice. |
| Triage event kinds | `_TRIAGE_KINDS = {ISSUE_OPENED, ISSUE_EDITED, ISSUE_REOPENED}` — matches the router. |
| Label catalog | Use the configured `triage.labels` keys from `.openbot/config.yaml`; no hard-coded label names. |

**Acceptance:**

1. `git log openbot/application/use_cases/triage.py` shows the label/priority
   commit on `main`.
2. `tests/application/use_cases/test_triage.py` covers:
   - bug + medium classification → calls `add_label` twice with the right
     names;
   - missing classifier output → ACK only, no label calls;
   - `add_label` raise → workflow still returns success and ACK still posts.
3. `triage.py` no longer says *"only the ACK is automated"* in `_ACK_TEMPLATE`.

---

### 2. Chat freeform answer has no tools

**Current state:**

`openbot/infrastructure/agents/deepagents_chat.py:116` —

```python
def build_tools(self, request: AgentRequest) -> Sequence[BaseTool]:
    return []
```

`@openbot help` and `@openbot stop|cancel` parsing already works
(`openbot/application/use_cases/chat_parser.py`). Freeform chat hits the
DeepAgent with zero tools, so it cannot answer repo-grounded questions.

The `read_file` / `grep_repo` implementations already exist in
`openbot/infrastructure/agents/_review_tools.py:42-90` (the review responder
uses them on `EvalChannelAdapter.files`). They are reusable.

**Required v0.1 alpha behavior:**

1. Chat profile registers a closed allowlist of read-only tools.
2. Tool input/output are bounded and path-allowlisted.
3. Chat cannot create branches, push, open PRs, label issues, edit files,
   merge, or run mutating shell commands.
4. Action requests (e.g. *"open a PR"*) receive a refusal that points the
   user to assignment.

**Tool allowance for v0.1:**

| Tool | Allowed | Source |
|---|---|---|
| `read_file(path)` | yes | reuse `_review_tools.py:42` (no sandbox needed) |
| `list_files(path=".")` | yes | new — bounded depth, bounded entries |
| `grep_repo(pattern, path_glob=None)` | yes | reuse `_review_tools.py:50` |
| `shell_readonly` | **no** | v0.1+ enhancement, requires argv allowlist + SSRF story |
| `web_fetch` | **no** | v0.1+ enhancement, requires SSRF allowlist |
| `write_file` / `shell_write` | **no** | fix workflow only |
| `gh_pr_create` / `gh_pr_merge` / `add_label` | **no** | never in chat |

**Locked decision:**

| Topic | Decision |
|---|---|
| File source | Chat tools read from `ChannelAdapter.read_file` (GitHub Contents API) for production; from `EvalChannelAdapter.files` for evals — same `read_file` signature, different adapter. |
| Output budget | Each tool truncates results at 8 KB and emits a `truncated=true` marker. The agent prompt explains the truncation contract so the model does not misinterpret partial results as the full file. |
| Path allowlist | Repo-relative only; reject paths starting with `/`, containing `..`, or matching the deny list (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `*.cert`). |
| Refusal copy | When the user asks for a state-changing action, reply with a one-line refusal + actionable hint (*"Chat is read-only — assign this issue to @openbot to trigger fix."*). |

**Acceptance:**

1. `tests/infrastructure/agents/test_deepagents_chat.py::test_build_tools_lists_read_only`
   asserts the three tool names are registered.
2. `test_chat_refuses_action_request` asserts *"open a PR"* gets a refusal
   string and **no** sandbox creation, **no** `create_pr` call.
3. `test_chat_grounded_answer` exercises a "where is config loaded?" question
   end-to-end with a fake adapter and asserts at least one `read_file` /
   `grep_repo` invocation appears in the message trace.
4. Path allowlist test: `read_file("../../../../etc/passwd")` returns a
   typed `ToolError`, not the host's file content.
5. `read_file(".env")` is rejected by the deny list before adapter call.

---

### 3. Output egress scanning not enforced

**Current state:**

- Input-side: `openbot/infrastructure/llm/sanitize.py` wraps user content with
  `wrap_user_input(...)` to defeat prompt injection — present and used.
- Sandbox-side: `_redact_tokens(...)` strips `x-access-token:<value>@` from
  git output before logging.
- **Egress-side: nothing.** Bot-authored text (review bodies, PR titles, fix
  failure replies, chat answers) goes straight to `ChannelAdapter.reply` /
  `create_pr_review` / `create_pr`.

CI / dev-side TruffleHog hooks exist, but they protect committed code, not
runtime model output.

**Required v0.1 alpha behavior:**

Every bot-authored string passes through **one** function — call it
`scan_egress_text(text, *, surface) -> SafeOutput` — before adapter call:

1. Issue/PR replies (`reply`).
2. PR review body and inline review comments (`create_pr_review`).
3. PR title and body if model-influenced (`create_pr`).
4. Fix failure-test snippets (`reply` again).
5. Triage classification comments.

If the scanner finds a **verified** secret pattern, it either redacts (default)
or blocks egress with a safe fallback (configurable). If the scanner times
out, fail-safe: drop the suspicious chunk, emit a one-line replacement, log
audit event `egress_scanner_timeout`.

**Locked decision:**

| Topic | Decision |
|---|---|
| Library | `detect-secrets` (Yelp), pinned. Pure Python, fast, secure-default; matches the repo's existing tldrsec security guidance preference for "vetted, well-tested libraries over custom solutions". TruffleHog requires a Go binary and is a poor runtime dep. |
| Boundary location | `openbot/application/middleware/egress_scan.py` — wraps the `ChannelAdapter` outbound calls via a thin adapter decorator, so handlers never call the scanner directly. |
| Default action | Redact (`<openbot:redacted-secret>`) and emit `audit_log` row `egress_redacted`. Configurable to block via `safety.egress_action: block` in `.openbot/config.yaml`. |
| Timeout | 500 ms per call; on timeout the entire chunk is replaced with a fixed safe string and the audit row records `egress_scanner_timeout`. |
| Surface enum | Closed `EgressSurface` enum — same pattern as `UserInputSource` in `sanitize.py`. Free-form strings rejected at the type level. |

**Acceptance:**

1. `tests/application/middleware/test_egress_scan.py::test_redacts_aws_key`
   shows a fake AWS key in a review finding becomes
   `<openbot:redacted-secret>` before `create_pr_review` is called.
2. `test_redacts_in_fix_test_output` shows a fake token in fix's failed-test
   snippet is redacted before `reply`.
3. `test_timeout_replaces_chunk` shows a 1 s scanner stub causes the chunk to
   be replaced with the safe fallback and an audit event recorded.
4. `test_block_mode` shows `egress_action: block` returns no PR review at all,
   replaced by a single audit comment.
5. No call site reaches the live `ChannelAdapter.reply` / `create_pr_review`
   / `create_pr` without going through `scan_egress_text`. Enforced by an
   import-graph test (`tests/architecture/test_egress_boundary.py`).

---

### 4. Agent-loop per-task budget not enforced

**Current state:**

- Preflight middleware checks monthly repo soft cap and global hard kill —
  before workflow start.
- `openbot/infrastructure/llm/complete.py` records cost per `complete(...)`
  call into `cost_meter`.
- **Inside the DeepAgents loop, nothing checks the running per-task spend
  before the next LLM/tool step.** A single runaway task can blow the per-task
  cap by orders of magnitude before the next workflow start checks it.

**Required v0.1 alpha behavior:**

1. Before every LLM call **and** before every tool call inside the agent loop,
   a `BudgetGuard.check()` is evaluated.
2. If `cost_meter.task_spent_usd >= per_task_cap_usd`, the loop emits one
   bounded message *"Per-task budget exceeded ($X / $Y); stopping."* and
   returns a partial outcome instead of continuing.
3. The guard is a `RuntimeMiddleware` registered once on the
   `BaseDeepAgentRuntime`, so every responder (review/fix/chat/triage)
   inherits it without per-handler wiring.
4. The guard is fail-safe: if the cost-meter read fails, log + continue;
   never block the agent because the budget store is unreachable.

**Locked decision:**

| Topic | Decision |
|---|---|
| Implementation site | New file `openbot/infrastructure/agents/_budget_middleware.py`. Mirrors `_middleware.py` shape. |
| Configuration | Reads `safety.budget.per_task_cap_usd` from `.openbot/config.yaml` (default $1.50). |
| Granularity | Check **before** each LLM call and **before** each tool call. Not after — by the time we know the cost, the spend already happened. Pre-check uses the running `cost_meter.task_spent_usd` as of the last completed call. |
| Failure mode on guard exceeded | Return `partial=True` outcome + post a one-line GitHub comment via the egress-scanned channel. Do not raise — handlers must still close the audit row cleanly. |
| Cost source | Same `cost_meter` table the preflight middleware reads. Single source of truth. |

**Acceptance:**

1. `tests/infrastructure/agents/test_budget_middleware.py::test_blocks_before_next_llm_call`
   stubs `cost_meter.task_spent_usd` past the cap and asserts the runtime
   stops before the next `acompletion` call.
2. `test_blocks_before_next_tool_call` same shape for tool dispatch.
3. `test_partial_outcome_on_exceed` asserts the responder returns
   `partial=True` and the audit row records `budget_exceeded_in_loop`.
4. `test_failsafe_on_meter_error` asserts the loop continues if the cost-meter
   query raises.
5. Integration: a synthetic 100-step fix loop with a $0.10 cap stops at step
   N, posts one bounded user-visible comment, and never executes step N+1.

---

## Cross-cutting cleanup

These are small but should land with the four workstreams above:

| Item | Action |
|---|---|
| PRD broken link | `docs/prd/openbot-prd.md:5` points to `../superpowers/specs/2026-05-22-v0-1-product-closure-design.md` which now lives at `../_archive/superpowers/...`. Fix the path in the same PR as workstream 1. |
| Stale TODO comment | `triage.py:15-16` claims the auto-label pipeline "still lands in upcoming commits" — remove once workstream 1 lands. |
| ACK template wording | `_ACK_TEMPLATE` in `triage.py:35-39` says "only the ACK is automated so far" — rewrite to reflect post-workstream-1 reality. |

No new docs, no new `_archive/` entries until this spec itself completes.

---

## Implementation order

| Order | Workstream | Why first |
|---|---|---|
| 1 | Triage label + priority on main | Smallest delta (cherry-pick + tests). Unblocks the alpha-readiness comment about triage being "ACK only". |
| 2 | Output egress scanner | Must wrap **before** chat tools land, otherwise grounded chat answers can leak file contents on first run. |
| 3 | Chat read-only tools | Depends on #2 because chat replies are now adapter-bound and need egress protection. |
| 4 | Agent-loop budget guard | Touches every responder; lands last so workstreams 1-3 don't have to deal with mid-loop aborts during tests. |

Each workstream is its own PR. No cross-cutting commits.

---

## Alpha readiness delta

Closing this spec satisfies the alpha-readiness checklist items the prior
spec left open:

| Item from prior spec | Closed by |
|---|---|
| "Triage handler posts an ACK only" | Workstream 1 |
| "Freeform chat uses a DeepAgent with no tools" | Workstream 3 |
| "Bot-authored GitHub output is not clearly scanned before egress" | Workstream 2 |
| "Per-task agent-loop enforcement is not yet at each LLM/tool step" | Workstream 4 |

After all four workstreams land:

1. `make check` passes.
2. `make -C evals test` passes.
3. A signed-webhook smoke can run `issues.opened → triage labels visible`,
   `pull_request.opened → review`, `issue assigned → fix`, `@openbot help` /
   `@openbot stop` / `@openbot where is config loaded?`.
4. A synthetic AWS key in any of those four flows does not reach
   `ChannelAdapter`.
5. A 100-step runaway loop stops at the per-task cap and posts one bounded
   user-visible reply.

Only at that point may the README and PRD `1.2 Alpha Readiness` checklist be
updated to "v0.1 alpha runnable".

---

## Non-goals

Out of scope for this spec; do not let scope creep pull them in:

1. Triage sandbox-reproduce (PRD §4.1) — defer to v0.2.
2. Chat `shell_readonly` and `web_fetch` — defer to v0.1+ once SSRF allowlist
   exists.
3. CI failure self-fix loop in fix workflow.
4. Persistent warm sandbox cache.
5. Linear / Slack / Discord adapters.
6. Issue dedup, automatic close, maintainer triage policy learning.
7. PyPI plugin sandbox.

---

## Follow-up plan requirements

The implementation plan generated from this spec must:

1. Land tests **before** code in each workstream (TDD; matches repo
   convention).
2. Use `uv run pytest` for tests; never bypass `make hooks`.
3. Use **fake adapters / fake sandboxes** for fast tests; live Daytona / live
   GitHub stays manual smoke-only.
4. Keep commits commit-sized (≤ ~400 lines diff each); one workstream = one
   PR, four PRs total.
5. Pin `detect-secrets` to an exact version (no open ranges).
6. Each PR updates `CHANGELOG.md` under `## [Unreleased]`.
7. The final PR (workstream 4) flips the README "current alpha status"
   section.
