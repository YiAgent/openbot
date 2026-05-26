# Issue-Reproduce Agent — Design Spec

**Status:** Reviewed — ready to plan
**Date:** 2026-05-24 (review pass: 2026-05-25)
**Branch:** `refactor/evals-runtime-openbot-harness`
**Review pass applied:**
- A7 added — `derive_sandbox_policy` tightens to "REQUIRED iff bug+repro_info" so sandbox lifetime matches the predicate.
- Invariants moved from `__post_init__` to tests (matches `domain/fix.py` convention).
- `_safe_update` removed in favor of extending the existing `sticky_reply` helper with `fallback_on_update_error`.
- Trace span name → `repro` (drops `_profile` suffix).
- `write_repro_script` deferred to v0.2; v0.1 reproduce agent has no sandbox write surface.
- `build_tools` contract test forbidden set now matches the real `_fix_tools` names (`write_file`, `git_diff`, `search_files`).
- §7 open questions closed inline.
**Scope:** Activate the third step of the triage pipeline described in PRD §4.1
("如果 issue 是 bug 且已有明确复现步骤，进入 Daytona sandbox 做 bounded
reproduce 并把简短证据贴回 issue").

---

## 1. Purpose & Non-goals

### 1.1 Purpose

Turn the placeholder triage ACK into an end-to-end pipeline that — for
`type=bug` issues with reproduction info — runs a bounded agent inside the
existing pre-provisioned sandbox, produces a structured `ReproOutcome`, and
edits the same sticky comment to show the result.

### 1.2 Non-goals (out of scope for this slice)

- Issue dedup, auto-close, or learning maintainer-private triage policy
  (PRD §4.1 explicitly punts these).
- Label / priority pipeline upgrades (`classify_labels` and `summarize`
  stages). They remain dead code on the `stages_to_run` path until a
  follow-up slice.
- Eval suite for reproduce (`evals/tasks/reproduce_*.py`,
  `evals/solvers/repro.py`, `evals/scorers/repro_score.py`).
- Pushing the repro artifact as a commit (v0.2 fix-handoff territory).
- Cost meter that enforces `$0.20 / issue` at runtime; we rely on
  `wall_seconds + model_call_limit` as proxy ceilings and on
  LangSmith/Langfuse for post-hoc cost auditing.

---

## 2. Architecture

### 2.1 Where reproduce lives in the existing pipeline

```
┌─────────────── Webhook async segment ───────────────┐
│ decide_and_enqueue                                  │
│  ├─ preflight chain                                 │
│  ├─ direct_actions (rule-based short-circuit)       │
│  ├─ classify_for_dispatch ──► TriageClassifierOutput│
│  ├─ derive_sandbox_policy   ──► REQUIRED / NO_SANDBOX│
│  └─ TaskSpec v3 ──► Redis Stream                    │
└─────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────── Worker ─────────────────────────────┐
│ execute_handler                                    │
│  └─ _run_with_sandbox  (pre-provisions handle)     │
│      └─ maybe_run_triage  ◄── changed              │
│          ├─ render_thinking_comment → adapter.reply│
│          ├─ if _should_run_reproduce(ctx):         │
│          │   ├─ adapter.get_issue                  │
│          │   ├─ DeepAgentsReproResponder           │
│          │   └─ render_final_comment(outcome)      │
│          │       → adapter.update_comment          │
│          └─ audit_lifecycle (Workflow.TRIAGE)      │
└────────────────────────────────────────────────────┘
```

### 2.2 Architectural decisions

| ID | Decision | Rationale |
|----|----------|-----------|
| A1 | Reproduce is a **stage inside `Workflow.TRIAGE`**, not a new workflow. | Shares the same `audit_lifecycle` row and the same `$0.20 / issue` budget bucket as PRD §4.1 prescribes. |
| A2 | Reproduce runs as a **new `ReproProfile` on the shared `runtime.py`**. | Inherits LangSmith trace, middleware limits, checkpointer plumbing for free; siblings with `FixProfile` / `ReviewProfile` / `ChatProfile`. |
| A3 | The `triage.py` use case grows **one new branch**; the existing ACK path becomes the no-sandbox / non-bug fallback. | Minimum blast radius; existing tests for the ACK path keep passing. |
| A4 | `ReproOutcome` is a **new value type at `openbot/domain/repro.py`**, peer to `domain/fix.py`. | Frozen, zero I/O, importable from both infra (agent) and application (use case + render) layers. |
| A5 | Reproduce triggers off `ctx.classifier_output` **directly**; `stages_to_run` plumbing stays dead. | `stages_to_run` is currently end-to-end dead code (worker never reads it). Activating it would touch 5 files for zero behavioural gain. |
| A6 | Reproduce uses a **sticky comment** via the existing `sticky_reply` helper in `_lifecycle.py`. | The helper already posts the placeholder, swallows update errors, and no-ops if the initial POST failed. Reuse instead of reinventing. |
| A7 | `derive_sandbox_policy` is **tightened** so triage requires a sandbox iff `type == "bug" AND has_reproduction_info`. | Without this, `type ∈ {feature_request, other}` returns REQUIRED and burns ~30s of Daytona/Modal time per non-bug issue with no reproduce step to run. Policy and `_should_run_reproduce` must agree. |

### 2.3 Locked boundaries (not touched in this slice)

- `openbot/dispatcher/classifier.py` — `TriageClassifierOutput` already
  carries `has_reproduction_info` + `type`; no schema change.
- `openbot/dispatcher/decide.py`,
  `openbot/application/dispatcher.py` — sandbox provisioning + classifier
  rehydrate are already wired.
- `openbot/application/sandbox_policy.py` — touched per A7; see §4.2.
- `openbot/infrastructure/queue/*` — `TaskSpec` schema unchanged,
  `stages_to_run` remains dead state.
- `openbot/application/middleware/preflight.py` — `PreflightContext`
  gains no new field.
- `openbot/infrastructure/adapters/github.py` — `reply` + `update_comment`
  cover everything we need.

---

## 3. Components

### 3.1 New files

| File | Responsibility | LOC est. |
|------|----------------|----------|
| `openbot/domain/repro.py` | `ReproStatus` enum + frozen `ReproOutcome` dataclass with invariants. | ~70 |
| `openbot/infrastructure/agents/_repro_schema.py` | Pydantic `ReproOutcomeSchema` for structured agent output. | ~50 |
| `openbot/infrastructure/agents/_repro_tools.py` | Safe sandbox tool subset (`read_file` / `list_files` / `run_command`). Imports the read-only wrappers from `_fix_tools` to keep tool semantics identical. | ~60 |
| `openbot/infrastructure/agents/deepagents_repro.py` | `ReproProfile` + `DeepAgentsReproResponder`. | ~180 |
| `openbot/application/use_cases/_repro_render.py` | Pure functions: `render_thinking_comment()`, `render_final_comment(outcome, *, ack_only=False)`. | ~80 |

### 3.2 Changed files

| File | Change | Δ LOC |
|------|--------|------:|
| `openbot/application/use_cases/triage.py` | Add reproduce branch + `_should_run_reproduce`, `_generate_repro_outcome` helper, switch ACK path to `sticky_reply(fallback_on_update_error=True)`. | +90 |
| `openbot/application/use_cases/_lifecycle.py` | Extend `_StickyReply` with `fallback_on_update_error: bool = False` flag (default keeps existing fix.py behaviour). | +12 |
| `openbot/application/sandbox_policy.py` | Tighten triage branch per A7 (bug+repro_info ⇒ REQUIRED; everything else ⇒ NO_SANDBOX). | ~6 |
| `openbot/infrastructure/agents/__init__.py` | Export `DeepAgentsReproResponder`. | +2 |
| `openbot/domain/__init__.py` (if present) | Export `ReproOutcome` / `ReproStatus`. | +2 |

### 3.3 ReproOutcome value type

```python
class ReproStatus(StrEnum):
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    INSUFFICIENT_INFO = "insufficient_info"
    AGENT_FAILED = "agent_failed"

@dataclass(frozen=True, slots=True)
class ReproOutcome:
    status: ReproStatus
    summary: str                       # 1-2 sentence comment-friendly summary
    command: str | None                # repro command (reproduced/not_reproduced only)
    exit_code: int | None              # exit code of `command`
    output_excerpt: str                # truncated to 2000 chars
    hypothesis: str                    # agent's root-cause guess OR list of missing info
    repro_artifact: str | None         # inline script body (reproduced only)
    repro_artifact_filename: str | None  # suggested filename for v0.2 fix handoff
```

**Invariants are asserted in tests, not `__post_init__`** — mirrors
`domain/fix.py`'s explicit convention. Partial-failure outcomes (e.g.
`AGENT_FAILED` after `command` ran but before `exit_code` was captured)
must still be representable so the audit row records what we actually
saw. The properties tests assert are:

- `repro_artifact` and `repro_artifact_filename` are both `None` or both set.
- `status == REPRODUCED` ⇒ `command is not None and repro_artifact is not None`.
- `status == INSUFFICIENT_INFO` ⇒ `command is None and repro_artifact is None`.
- `len(output_excerpt) <= 2000`.

### 3.4 ReproProfile limits

```python
limits = AgentRunLimits(
    recursion_limit=40,        # cf. Fix 200, Chat 100
    model_call_limit=15,
    tool_call_limit=30,
    wall_seconds=180,          # hard ceiling — Fix has none
    max_output_tokens=4000,
    thinking_budget_tokens=0,
)
```

### 3.5 Tool whitelist (security invariant)

`_repro_tools.py` exposes **only** these tools to the agent:

- `read_file(path)` — read-only
- `list_files(path=".", max=200)` — read-only (matches `_fix_tools` name)
- `run_command(command: list[str], timeout_seconds: int = 60)` — bounded shell execution

Explicitly **forbidden** (asserted by a contract test):
`{"write_file", "git_diff", "search_files"}` — the actual mutation-or-leak-capable
fix-tool names from `_fix_tools.make_fix_tools`. Asserting against
*real* tool names (not invented strings like `apply_patch`) keeps the
contract test honest.

**`write_repro_script` is deferred to v0.2** alongside the fix-handoff
work (§1.2). The agent holds the script body in
`ReproOutcome.repro_artifact` (just a string in the structured output).
Avoiding any sandbox write removes the path-traversal attack surface
for v0.1; the same string travels into v0.2 as the on-disk
`repro_<status>.sh` when fix-handoff lands.

---

## 4. Data flow

### 4.1 End-to-end happy path

| Hop | From | To | Payload |
|-----|------|-----|---------|
| ① | `UnifiedEvent.raw.issue.body` | `TriageClassifierOutput` | `{type, severity_guess, has_reproduction_info, looks_like_spam}` |
| ② | typed → dict | Redis Stream (`TaskSpec.classifier_output`) | `dataclass_asdict()` |
| ③ | dict → typed | `execute_handler(classifier_output=...)` | `parse_classifier_output()` |
| ④ | `PreflightContext` | `maybe_run_triage` | `ctx.classifier_output`, `ctx.sandbox_handle`, `ctx.adapter`, `ctx.event` |
| ⑤ | `render_thinking_comment()` | GitHub comment | markdown str |
| ⑥ | `adapter.reply()` response | local `comment_id: int` | `response["id"]` |
| ⑦ | `get_issue() + handle.checkout.ref` | `AgentRequest.input` | `{issue_title, issue_body, base_sha}` |
| ⑧ | LangGraph terminal state | `ReproOutcome` | dataclass |
| ⑨ | `render_final_comment(outcome)` | str | markdown |
| ⑩ | `(comment_id, body)` | GitHub `PATCH /issues/comments/{id}` | 204 No Content |
| ⑪ | `outcome.status.value` | `task_runs.outcome` | text column |

### 4.2 Reproduce-trigger predicate (pure)

```python
def _should_run_reproduce(ctx: PreflightContext) -> bool:
    if ctx.sandbox_handle is None:
        return False
    output = ctx.classifier_output
    if not isinstance(output, TriageClassifierOutput):
        return False
    return output.type == "bug" and output.has_reproduction_info
```

This now agrees with `derive_sandbox_policy` post-A7 — the policy is
the *primary* gate (no sandbox provisioned for non-reproducible
triage), and this predicate is a defence-in-depth check inside the use
case so a future policy regression doesn't leak a sandbox handle into a
non-reproduce run. **One rule, asserted twice; both sites must agree —
covered by `tests/application/test_sandbox_policy.py` (existing) +
`tests/application/use_cases/test_triage_reproduce.py::test_predicate_matches_policy`
(new, ~6-row matrix).**

The corresponding policy change in `sandbox_policy.py`:

```python
# Before
_TRIAGE_BYPASS_TYPES_WITHOUT_REPRO = frozenset({"spam", "question"})
# ...
if (output.type in _TRIAGE_BYPASS_TYPES_WITHOUT_REPRO
        and not output.has_reproduction_info):
    return SandboxPolicy.NO_SANDBOX

# After
if feature is Feature.TRIAGE and isinstance(output, TriageClassifierOutput):
    if output.looks_like_spam:
        return SandboxPolicy.NO_SANDBOX
    # Triage requires sandbox iff we'll actually run reproduce.
    if not (output.type == "bug" and output.has_reproduction_info):
        return SandboxPolicy.NO_SANDBOX
```

### 4.3 Sandbox lifecycle

| Stage | Owner |
|-------|-------|
| Provision (clone + token + handle) | dispatcher `_run_with_sandbox` |
| Use | `DeepAgentsReproResponder` (via `AgentRequest.sandbox`) |
| Cleanup | dispatcher's context manager |
| Cache | `SandboxCachePort` (existing) |

The reproduce use case **never** calls `sandbox.commit_and_push`,
`adapter.create_branch`, or `adapter.open_pull_request`.

### 4.4 Checkpoints (subset of fix.py)

- After `get_issue` — cancellation point.
- After agent loop — cancellation point.
- (Skip `create_branch` / `push` — reproduce makes no GitHub mutations.)
- `finally`: `checkpointer.adelete_thread(run_id)` wrapped in
  `try/except Exception` and `if checkpointer is not None` (same
  hygiene as `fix.py:343` — checkpointer is None in unit tests).

### 4.5 Trace shape (LangSmith + Langfuse)

| Layer | Name | Parent |
|-------|------|--------|
| use case | `triage` (existing `@_observe + @_traceable`) | webhook span |
| profile | `repro` (= `ReproProfile.agent_name`, matches `review` / `fix` / `chat`) | `triage` |
| LangGraph nodes | `read_issue`, `inspect_repo`, `attempt_repro`, `decide_outcome` | `repro` |
| tool calls | `read_file`, `list_directory`, `run_command` | LangGraph node |

LangSmith metadata: `{issue_number, repo, classifier_type,
has_reproduction_info, repro_status_final}` for downstream dashboards.

---

## 5. Failure semantics

### 5.1 Error taxonomy

| Class | Examples | Use case behaviour | User sees |
|-------|----------|--------------------|-----------|
| **Infrastructure** | network jitter, sandbox boot, Daytona 5xx, `AgentTimeoutError`, `AgentBudgetExhaustedError`, `AgentStructuredOutputError`, `AgentExecutionError` | catch, write `audit.outcome = f"reproduce:agent_failed:{type(exc).__name__}"`, render fallback | `agent_failed` template |
| **Agent judgement** | `INSUFFICIENT_INFO`, `NOT_REPRODUCED` | write `audit.outcome = "reproduce:<status>"`, normal render | corresponding template |
| **User action** | `RunCancelledError` (cancel-openbot label) | `BaseException` propagates through `audit_lifecycle` → CANCELLED | comment stays in `thinking` state |

### 5.2 Failure matrix

| Failure point | Behaviour | Comment outcome |
|---------------|-----------|-----------------|
| webhook classifier exception | fail-open → `classifier_output=None` | `_should_run_reproduce → False`, ack_only |
| sandbox provisioning failed | `sandbox_handle=None` | ack_only (dispatcher already counted `bypass_source=degrade`) |
| `sticky_reply` placeholder failed (initial POST) | `comment_id=None` → all later `sticky.update(...)` are no-ops; agent run still executes and result is logged. `audit.outcome="reproduce:<status>:no_comment"` | no comment (sandbox already provisioned — work continues, result lands in audit + Langfuse only) |
| `adapter.get_issue` failed | `ReproOutcome(status=AGENT_FAILED, ...)` | sticky → `agent_failed` |
| agent timeout / budget exhausted | `AgentTimeoutError` / `AgentBudgetExhaustedError` → AGENT_FAILED | sticky → `agent_failed` |
| agent early-exit on insufficient info | `ReproOutcome(status=INSUFFICIENT_INFO, ...)` | sticky → `insufficient_info` |
| `update_comment` failed | fallback to `reply()` (accepted: two comments) | thinking + final |
| cancellation | `RunCancelledError` propagates to audit | sticky stays in `thinking` |

### 5.3 `AGENT_FAILED` comment policy

The user-visible message reads:

> :robot: OpenBot's reproduce agent encountered an error and has been
> logged. Please re-trigger by re-opening the issue if you'd like to
> retry.

The exception class name is **not** exposed in the comment (avoids
ops-info leak). Full type + message live in the audit row and logs.

### 5.4 Budget enforcement

| Layer | Knob | Effect |
|-------|------|--------|
| profile | `wall_seconds=180` | `AgentTimeoutError` |
| profile | `model_call_limit=15` | `AgentBudgetExhaustedError` |
| profile | `tool_call_limit=30` | `AgentBudgetExhaustedError` |
| profile | `max_output_tokens=4000` | model self-truncates |
| profile | `recursion_limit=40` | LangGraph raise → `AgentExecutionError` |

No new runtime `$0.20` meter — relies on the proxy ceilings above and
post-hoc LangSmith/Langfuse cost audit.

### 5.5 Cancellation contract

`RunCancelledError` inherits `BaseException` (not `Exception`). The use
case's `try/except Exception` block deliberately cannot swallow it. The
sticky comment is **not** updated on cancellation — the cancel
direct-action path has already posted "🚫 cancelled" to the issue;
double-updating would be noise.

### 5.6 `update_comment` fallback — reuse `sticky_reply`

Do **not** introduce a parallel `_safe_update` helper. `openbot/application/use_cases/_lifecycle.py`
already exports `sticky_reply`, which:

- Posts the placeholder outside any try/except (so auth/network failure
  at start lands in logs, not as a silent skip).
- Yields a `_StickyReply(comment_id)` whose `.update(body)` is a silent
  no-op when `comment_id is None`.
- Swallows `update_comment` errors via `_logger.exception(...)`.

The only behavioural delta this slice needs is "fall back to a second
`reply()` on update failure". Extend the helper with one flag rather
than copy-pasting it:

```python
# _lifecycle.py — minimal extension to existing helper
class _StickyReply:
    fallback_on_update_error: bool = False  # new, defaults to current behaviour

    async def update(self, message: str) -> None:
        if self.comment_id is None:
            return
        try:
            await self._adapter.update_comment(self._event, self.comment_id, message)
        except Exception:
            _logger.exception("sticky_reply_update_failed", extra={"comment_id": self.comment_id})
            if self.fallback_on_update_error:
                try:
                    await self._adapter.reply(self._event, message)
                except Exception:
                    _logger.exception("sticky_reply_fallback_failed", ...)
```

Triage's reproduce branch passes `fallback_on_update_error=True`. Fix's
existing call sites keep the default. Acceptable trade-off: two comments
instead of one is strictly better than the user seeing only
"investigating…" forever.

### 5.7 Known limitations (documented, not fixed)

- Issue body edits between webhook delivery and `get_issue` fetch:
  we use the latest fetched body, do not reconcile against the cached
  classifier output.
- GitHub API integrated rate limit shared with REPLY/UPDATE: adapter
  retries internally; exhaustion lands in `sticky_reply`'s
  `fallback_on_update_error` branch (one extra `reply()` call).
- audit-log *write* failures are swallowed inside `_lifecycle._write_phase`
  (`_lifecycle.py:142`); workflow-body exceptions still propagate
  through `audit_lifecycle` after the FAILED row is written.
- Multiple `issue.opened` deliveries (e.g. reopen): each delivery posts
  its own placeholder; we do not search for a prior bot comment.

---

## 6. Testing strategy

### 6.1 Pyramid

| Layer | Count | Path |
|-------|------:|------|
| Unit (domain / render / profile) | ~20 | `tests/domain/`, `tests/application/use_cases/`, `tests/infrastructure/agents/` |
| Use case integration | ~6-8 | `tests/application/use_cases/test_triage_reproduce.py` |
| E2E (Docker sandbox + fake LLM) | 1 | `tests/e2e/test_triage_repro_e2e.py` |
| Eval | — | **out of scope, deferred to v0.1.x** |

### 6.2 Unit tests

#### `tests/domain/test_repro.py` (~5)
- `ReproStatus` enum values stable (guards serialization).
- `ReproOutcome.frozen=True` (mutation raises `FrozenInstanceError`).
- Missing required fields raise `TypeError`.
- Invariant: `repro_artifact` and `repro_artifact_filename` both-or-neither.
- Invariant: `status==REPRODUCED` requires `command` and `repro_artifact`.

#### `tests/application/use_cases/test_repro_render.py` (~8)
- `render_thinking_comment()` non-empty, contains `:robot:` (ACK tone).
- `render_final_comment(outcome=None, ack_only=True)` ≡ existing ACK text.
- Snapshot per status: `:white_check_mark:` / `:warning:` /
  `:speech_balloon:` / `:robot:`.
- `output_excerpt > 2000` chars truncated with `[truncated]` marker.
- `repro_artifact` rendered as `<details>` block with fenced code.
- Adversarial body (`</details><script>`) is HTML-escaped or
  fence-broken (XSS defence).

#### `tests/infrastructure/agents/test_deepagents_repro.py` (~7)
- `ReproProfile` attributes: `feature == Feature.TRIAGE`,
  `agent_name == "repro"`, `sandbox_requirement == REQUIRED`,
  `checkpoint_enabled == True`.
- `system_prompt(request)` contains early-exit instruction
  ("if the issue description lacks ... return INSUFFICIENT_INFO
  immediately").
- `user_message(request)` contains `issue_title + issue_body + base_sha`.
- **`build_tools(request)` contract test**: returned tool `.name`s ⊆
  `{"read_file", "list_files", "run_command"}`, and the returned set
  is disjoint from `{"write_file", "git_diff", "search_files"}` — the
  real mutation-or-leak fix-tool names exposed by
  `_fix_tools.make_fix_tools`. Asserting against real names (not
  invented strings) prevents the test from passing vacuously.
- `parse_result()` accepts a valid `ReproOutcomeSchema` dict → `ReproOutcome`.
- `parse_result()` missing fields → `AgentStructuredOutputError`.
- `limits` values match the spec table in §3.4.

### 6.3 Use case integration

`tests/application/use_cases/test_triage_reproduce.py` — each test
builds a `PreflightContext` from the existing `preflight_ctx_factory`
fixture, mocks `adapter`, and monkeypatches `_generate_repro_outcome`:

| Test | classifier_output | sandbox_handle | Expected audit | Expected comments |
|------|-------------------|----------------|----------------|-------------------|
| `test_no_classifier_falls_back_to_ack` | `None` | `None` | `ack_only` | thinking → ack_only |
| `test_non_bug_type_skips_repro` | `type=question` | `None` | `ack_only` | thinking → ack_only |
| `test_bug_without_repro_info_skips` | `type=bug, has_repro=False` | `None` | `ack_only` | thinking → ack_only |
| `test_bug_with_repro_info_runs_agent` | `type=bug, has_repro=True` | provisioned | `reproduce:reproduced` | thinking → reproduced |
| `test_agent_exception_renders_failed_template` | bug+repro | provisioned + `_generate_repro_outcome` raises | `reproduce:agent_failed` | thinking → agent_failed |
| `test_placeholder_post_failure_runs_agent_anyway` | bug+repro | provisioned + `adapter.reply` raises | `reproduce:<status>:no_comment` | none (`sticky_reply` swallowed initial POST; agent still ran; outcome in audit) |
| `test_update_comment_fails_falls_back_to_reply` | bug+repro | provisioned + `update_comment` raises | `reproduce:reproduced` | thinking + final (two comments) |
| `test_cancellation_propagates_and_writes_cancelled` | bug+repro | provisioned + `checkpoint` raises `RunCancelledError` | `CANCELLED` | thinking only, no update |

### 6.4 E2E

`tests/e2e/test_triage_repro_e2e.py` — one test, reusing
`tests/e2e/conftest.py` Docker sandbox + LocalChannelAdapter +
`fake_llm` fixture. **Pins the happy path only**:

- POST `issue.opened` webhook (bug + repro info) → wait for worker.
- Assert `LocalChannelAdapter` received `reply` exactly once
  (placeholder) + `update_comment` exactly once (final).
- Assert final body contains `:white_check_mark:` or `:warning:`.
- Assert `audit_log` row exists with `outcome LIKE 'reproduce:%'`.

The `update_comment`-fails fallback path (two `reply` calls) lives in
`test_triage_reproduce.py::test_update_comment_fails_falls_back_to_reply`
(use case integration, no Docker), not in E2E. Real LLMs are **not**
invoked in CI — deferred to a future nightly eval job.

### 6.5 Coverage targets

| File | Target |
|------|--------|
| `domain/repro.py` | 100 % |
| `_repro_render.py` | 100 % |
| `deepagents_repro.py` | ≥ 85 % |
| `use_cases/triage.py` (changed lines) | ≥ 90 % |

### 6.6 CI budget

- Unit + use case: ~80 new tests → ~15 s local, ~30 s CI.
- E2E: 1 new test → ~45 s.
- No new network deps, no new secrets.

---

## 7. Decisions (formerly open questions)

1. **Trace name → `repro`** (closed). `ReproProfile.agent_name = "repro"`
   matches `review` / `fix` / `chat`. The `_profile` suffix is dropped.
2. **`AGENT_FAILED` audit format → `reproduce:agent_failed:<ExceptionClass>`**
   (closed). The exception class name is PII-free (already used by
   `audit_lifecycle` at `_lifecycle.py:202`) and lets Langfuse dashboards
   slice by timeout vs budget vs structured-output failure with no extra
   instrumentation. Plain `reproduce:agent_failed` would lose that signal.
3. **`repro_artifact` → inline string, capped at 2000 chars** (closed —
   same budget as `output_excerpt`). v0.2 fix-handoff (already deferred
   per §1.2) is when on-disk script storage earns its keep; until then
   inline keeps the audit row self-contained and avoids a second
   side-channel just for v0.1.

---

## 8. References

- PRD §4.1 (`docs/prd/openbot-prd.md`): triage pipeline.
- PRD §11: `reproduce_python_issue` / `reproduce_js_issue` listed as
  v0.2 community plugins — this slice is the first-party precursor.
- `openbot/infrastructure/agents/profiles.py`: `AgentProfile` protocol +
  `AgentRequest` / `AgentRunLimits` / error hierarchy that
  `DeepAgentsReproResponder` will plug into.
- `openbot/application/use_cases/fix.py`: reference for the
  use case shape (audit_lifecycle, checkpoint pattern, finally cleanup).
- `openbot/application/sandbox_policy.py`: gating logic that
  guarantees `sandbox_handle` is set when we reach reproduce.
