# Unified Sandbox Entry — All workflows enter sandbox at preflight (design)

**Status:** design. Awaiting review before plan.
**Date:** 2026-05-21
**Branch (proposed):** `feat/unified-sandbox-entry`
**PRD anchors:** §3 (locked sandbox boundary), §4.1 (triage repro), §4.2 (review), §4.3 (fix), §4.4 (chat), §6 (per-task budgets).
**Supersedes (in spirit):** Slice C's per-handler clone — moves provisioning up one layer.

---

## Goal

Replace the current "every workflow decides for itself whether to enter the sandbox" model with a **single sandbox-provisioning stage at the dispatcher layer**: every workflow handler receives a `SandboxedHandle` with the repository already cloned at the correct ref, OR an explicit `None` when sandbox is intentionally skipped (label-only triage, cancel events, sandbox-not-configured deployments).

Concretely:

1. After preflight clears, `dispatcher.run_dispatch` resolves a `CheckoutSpec` from `(event, workflow)`.
2. It opens a sandbox via the injected factory, clones at the resolved ref.
3. It calls the workflow handler with the sandbox attached.
4. Handler implements **only its workflow-specific logic** — no clone, no token, no factory.
5. On sandbox/clone failure: graceful degrade — handler is still called, but without a sandbox; handler decides the user-facing fallback message.

This unifies four currently-divergent code paths (fix has sandbox + clone; triage/review/chat do not) into one capability surface, and turns "different agents looking at different refs" into a pure data-resolution problem instead of a control-flow problem.

---

## Locked decisions

| Topic | Decision | Rationale |
|---|---|---|
| Provisioning layer | **`dispatcher.run_dispatch` after preflight, before `dispatch.handler(ctx)`** | Single source of truth; handlers stay focused on workflow logic. |
| Ref resolution | **`resolve_checkout(event, workflow, adapter) -> CheckoutSpec` — pure function + adapter I/O.** | The `(event_kind × workflow) → ref` matrix is the highest-risk surface (wrong ref = agent reads wrong code); making it a pure function lets us unit-test every cell without a sandbox. |
| Sandbox lifetime | **Per-event, async-with scoped, destroyed on handler exit.** | Carries forward Slice C's multi-tenant safety > cold-start trade-off. |
| Capability layering | **Workflow differentiation = tool set + system prompt + budget**, NOT a separate "has sandbox" flag. | Tools-not-in-toolset is the physical safety boundary; review/chat agents *cannot* `write_file` because the function isn't registered. |
| Bypass mechanism (static) | **`SandboxPolicy.NO_SANDBOX` per-dispatch flag** for cancel events, merged-PR events, label-only triage. | Provisioning every event would waste $$$ and time on triggers that never read code. |
| Bypass mechanism (dynamic) | **Existing LLM intent classifier (`openbot/dispatcher/classifier.py`) gets a second job: short-circuit to `NO_SANDBOX` when output indicates the event is non-productive** (`chat.intent ∈ {unclear, out_of_scope}`, `triage.looks_like_spam`, `triage.type ∈ {spam, question}` without repro info). | The classifier already runs per-event with 1h Redis cache; reusing its output avoids paying sandbox cost on events it has already classified as useless. Static + dynamic are OR-merged before provisioning. |
| Failure mode | **Graceful degrade: handler always called; sandbox handle may be `None`.** | Workflow-specific fallback copy lives in the handler; dispatcher only does sandbox-generic logging. |
| Capability port | **No change to `SandboxPort` shape.** `clone()` gains a `strategy: CloneStrategy` kwarg with a default. | Backward-compat for existing `FakeSandboxAdapter` / `DaytonaSandboxAdapter`; can be ignored by adapters that don't optimize. |
| Cache layer | **Out of scope for this slice.** Snapshot cache is a follow-up adapter-internal optimization. | Spec stays focused on the entry-point shape; cost optimization can land later inside `DaytonaSandboxAdapter.clone` without changing call sites. |
| Inline review comment refs | **Use `comment.commit_id`, not `pr.head.sha`.** | GitHub inline comments live at a specific historical commit; checking out head means line numbers drift. |

---

## Architecture

### Layer map (hexagonal contract preserved)

```
domain/
  checkout.py                       # NEW: CheckoutSpec, CloneStrategy
  events.py                         # GROWS: UnifiedEvent gains optional clone_url, review_commit_id, last_reviewed_sha

application/
  checkout_resolver.py              # NEW: resolve_checkout(event, workflow, adapter) -> CheckoutSpec
  sandbox_handle.py                 # NEW: SandboxedHandle dataclass (sandbox + checkout + token)
  ports/
    channel_adapter.py              # GROWS: get_default_branch_sha(event), get_pull_request(event, n)
    sandbox.py                      # GROWS: clone(... , strategy: CloneStrategy = SHALLOW)
  middleware/
    preflight.py                    # GROWS: PreflightContext gains optional sandbox_handle field (set by dispatcher)
  dispatcher.py                     # GROWS: post-preflight sandbox provisioning block
  router.py                         # GROWS: Dispatch gains sandbox_policy: SandboxPolicy

infrastructure/
  sandboxes/
    daytona.py                      # MINOR: clone() accepts strategy, switches between shallow / blobless / shallow_history
    fake.py                         # MINOR: clone() accepts strategy (no-op respect)
  agents/
    deepagents_triage.py            # NEW: DeepAgentsTriageResponder (read-only tools + run allowlist)
    deepagents_review.py            # NEW: DeepAgentsReviewResponder (read-only tools + lint/typecheck/test allowlist)
    deepagents_chat.py              # MODIFIED: tools=[read_file, list_files, grep_files], prompt reversed
    _triage_tools.py                # NEW
    _review_tools.py                # NEW
    _chat_tools.py                  # NEW (read-only subset of _fix_tools)
```

### Per-event flow (after this slice)

```
Webhook arrives
    ↓
dispatcher.run_dispatch
    ↓
preflight chain (sanitize → kill_switch → … → audit_start)
    ↓  decision = PROCEED
    ↓
[EXISTING] classifier_output = await classify_event(event, dispatch.feature)
    ↓                                         # cached 1h in Redis; fail-open returns None
[NEW] effective_policy = derive_sandbox_policy(
          static=dispatch.sandbox_policy,
          classifier_output=classifier_output,
          feature=dispatch.feature,
      )
    ↓
[NEW] if effective_policy is NO_SANDBOX:
          ctx_with_class = dataclasses.replace(ctx, classifier_output=classifier_output)
          await dispatch.handler(ctx_with_class)   # cancel, merged-PR, label-only, spam, unclear
          return
    ↓
[NEW] checkout = await resolve_checkout(event, dispatch.feature, adapter)
    ↓                                         # pure logic + a few adapter calls
[NEW] token = await adapter.get_installation_token(event)
    ↓
[NEW] async with ctx.sandbox_factory() as sandbox:
          await sandbox.clone(
              repo_url=checkout.repo_url,
              ref=checkout.ref,
              token=token,
              strategy=checkout.strategy,
          )
          sandboxed_ctx = dataclasses.replace(
              ctx,
              sandbox_handle=SandboxedHandle(sandbox, checkout, token),
              classifier_output=classifier_output,
          )
          await dispatch.handler(sandboxed_ctx)
    ↓
(sandbox destroyed by async-with on every exit, including exceptions)
```

### Failure-degrade flow

```
checkout resolution fails (no rule for this trigger)  ──┐
get_installation_token fails                            ├──> log + handler(ctx without sandbox_handle)
sandbox factory not configured                          │      → handler chooses workflow-specific fallback reply
sandbox.clone fails                                     ──┘
```

The graceful-degrade contract:

> If `ctx.sandbox_handle is None`, the handler MUST still post some user-facing reply (a `_NO_SANDBOX` template or workflow-specific fallback). It MUST NOT raise — dispatcher already 202'd and the webhook will be retried by GitHub if we 5xx out.

This contract is identical to today's `fix.py` behavior, just generalized.

---

## Data model

### `CheckoutSpec` and `CloneStrategy`

```python
# openbot/domain/checkout.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import StrEnum


class CloneStrategy(StrEnum):
    """How much git materializes on clone. Cost: blobless ≪ shallow ≪ shallow_history ≪ full."""

    BLOBLESS = "blobless"              # --filter=blob:none — metadata only, lazy blob fetch on read
    SHALLOW = "shallow"                # --depth=1 — one commit, no history
    SHALLOW_HISTORY = "shallow_history"  # --depth=N — enough history for incremental diff
    FULL = "full"                      # full clone — only when git blame/log heavy required


@dataclass(frozen=True, slots=True)
class CheckoutSpec:
    """The exact (repo, ref, strategy) any workflow needs in its sandbox.

    Pure value type. No I/O. Constructed by ``resolve_checkout``; consumed
    by the dispatcher's clone step. Adapters interpret ``strategy`` —
    ``FakeSandboxAdapter`` is allowed to ignore it (it uses a local tmpdir).
    """

    repo_url: str
    ref: str                           # The exact SHA to materialize (never a branch name).
    strategy: CloneStrategy = CloneStrategy.SHALLOW
    diff_base: str | None = None       # Incremental review: diff base SHA (not the checkout ref).
    sparse_paths: tuple[str, ...] = field(default_factory=tuple)  # Reserved for monorepo support.
```

### `SandboxedHandle`

```python
# openbot/application/sandbox_handle.py
from __future__ import annotations
from dataclasses import dataclass
from openbot.application.ports.sandbox import SandboxPort
from openbot.domain.checkout import CheckoutSpec


@dataclass(frozen=True, slots=True)
class SandboxedHandle:
    """Everything a workflow handler needs to operate on cloned code.

    ``token`` is exposed because FIX still needs it for ``commit_and_push``.
    Read-only workflows (triage/review/chat) should ignore it.
    """

    sandbox: SandboxPort
    checkout: CheckoutSpec
    token: str
```

### `SandboxPolicy`

```python
# openbot/application/router.py (additions)
class SandboxPolicy(StrEnum):
    """Per-dispatch bypass — set by the router when an event manifestly does not need code."""

    REQUIRED = "required"      # Standard path: provision + clone.
    NO_SANDBOX = "no_sandbox"  # Bypass: cancel events, merged-PR notifications, label-only triage.
```

`Dispatch` gains:
```python
sandbox_policy: SandboxPolicy = SandboxPolicy.REQUIRED
```

### `PreflightContext` extension

`PreflightContext` is `frozen=True` by Slice A's design discipline. We add:

```python
sandbox_handle: SandboxedHandle | None = None
classifier_output: ClassifierOutput | None = None  # union of Triage/Review/Chat output types
```

Dispatcher uses `dataclasses.replace(ctx, sandbox_handle=..., classifier_output=...)` to produce a new instance — never mutates.

The `classifier_output` field flows through to all handlers (including bypass paths) so that workflow-specific logic can specialize on it without re-running the LLM call. Examples:
- Triage handler reads `classifier_output.has_reproduction_info` to decide whether to attempt repro inside the sandbox.
- Review handler reads `classifier_output.suggested_subagents` to assemble its sub-agent fleet.
- Chat handler reads `classifier_output.intent` to choose between answer-now vs ask-for-clarification (the latter requires no sandbox).

### `UnifiedEvent` extensions

The ref resolver needs a few facts that today live only in `event.raw`. Promote them to typed fields:

| Field | Source | Used by |
|---|---|---|
| `clone_url: str \| None` | `payload.repository.clone_url` | All workflows (handed to `sandbox.clone`) |
| `review_commit_id: str \| None` | `payload.comment.commit_id` (inline review comments) | Inline review chat — checkout the historical commit |
| `last_reviewed_sha: str \| None` | DB lookup (`task_runs.last_reviewed_sha`) — already in slice F | Incremental review (sets `diff_base`) |

Existing ingest code already extracts most of these into `event.raw`; the change is mainly a typed surface.

---

## Intent classification integration

The existing per-event LLM classifier (`openbot/dispatcher/classifier.py`, Sonnet 4.6 + Redis cache, ~$0.001–0.005 / event) becomes the **second** source of `SandboxPolicy.NO_SANDBOX`. Static + dynamic are OR-merged before the dispatcher decides whether to provision the sandbox.

### Why this matters

Without classifier integration, every event that passes preflight goes through:
1. `resolve_checkout` (1–2 adapter calls)
2. `get_installation_token` (GitHub API call, may rotate)
3. `factory()` sandbox provisioning (Daytona: ~5–15 s cold-start)
4. `sandbox.clone` (shallow: ~3–10 s for typical repo)
5. Handler runs the responder
6. Sandbox destroyed

Steps 2–4 cost ~10–25 s wall-clock + cents per event. The classifier already knows at step 0 that a spam comment, an "I have no idea what you do" chat, or a duplicate-issue-style trigger will exit early at step 5 anyway. Skipping steps 2–4 for those events saves the entire sandbox bill on non-productive traffic.

### Current classifier behavior (verbatim)

`classify_event(event, feature)` returns one of:

| Feature | Output | Fields used for NO_SANDBOX decision |
|---|---|---|
| `triage` | `TriageClassifierOutput` | `looks_like_spam=True` OR `type ∈ {spam, question}` with `has_reproduction_info=False` |
| `review` | `ReviewClassifierOutput` | Never bypasses — review always needs the diff in-sandbox to ground findings. |
| `chat` | `ChatClassifierOutput` | `intent ∈ {unclear, out_of_scope}` |
| `fix` | (no classifier — `stages_from_classifier` always returns full pipeline) | Never bypasses — fix is user-explicit. |

Failure mode: classifier raises / times out → returns `None` → treat as "no dynamic signal" → fall back to static policy. The classifier MUST be fail-open; a transient LLM failure must not block legitimate events.

### The merge function

```python
# openbot/application/sandbox_policy.py  (new)
from __future__ import annotations
from openbot.application.router import SandboxPolicy
from openbot.dispatcher.classifier import (
    ClassifierOutput,
    TriageClassifierOutput,
    ChatClassifierOutput,
)
from openbot.domain.workflow import Workflow


def derive_sandbox_policy(
    *,
    static: SandboxPolicy,
    classifier_output: ClassifierOutput | None,
    feature: Workflow,
) -> SandboxPolicy:
    """OR-merge static router policy with dynamic classifier policy.

    NO_SANDBOX wins: if EITHER source says skip, we skip. The handler still
    runs (per the graceful-degrade contract), but with ``sandbox_handle is None``
    and can post a workflow-specific short reply (or no reply at all).
    """
    if static is SandboxPolicy.NO_SANDBOX:
        return SandboxPolicy.NO_SANDBOX

    if classifier_output is None:
        # Fail-open: no classifier signal → respect static policy
        return static

    if feature is Workflow.TRIAGE and isinstance(classifier_output, TriageClassifierOutput):
        if classifier_output.looks_like_spam:
            return SandboxPolicy.NO_SANDBOX
        if classifier_output.type in {"spam", "question"} and not classifier_output.has_reproduction_info:
            return SandboxPolicy.NO_SANDBOX

    if feature is Workflow.CHAT and isinstance(classifier_output, ChatClassifierOutput):
        if classifier_output.intent in {"unclear", "out_of_scope"}:
            return SandboxPolicy.NO_SANDBOX

    return SandboxPolicy.REQUIRED
```

### What handlers do with bypass + classifier output

| Workflow | `NO_SANDBOX + classifier_output` | Handler behavior |
|---|---|---|
| triage | `looks_like_spam` or `type=spam` | No reply — silently drop (or apply `triage:spam` label only). |
| triage | `type=question` no repro | Reply asking for reproduction steps; do NOT enter sandbox. |
| chat | `intent=unclear` | Reply asking for clarification (the existing chat fallback copy). |
| chat | `intent=out_of_scope` | Reply explaining scope; do NOT enter sandbox. |
| review | (never bypasses on classifier) | N/A |
| fix | (never bypasses on classifier) | N/A — fix uses static `NO_SANDBOX` only (e.g., assigned-but-no-write-perm). |

### Caching and idempotency

- The classifier already caches output for 1h in Redis keyed on `sha256(feature|body[:2000]|version)`.
- Cache hits cost 0 (no LLM call).
- `derive_sandbox_policy` is pure — no extra round trip.
- Net cost of classifier-driven bypass: ~$0 on cache hit, ~$0.001–0.005 on miss. Net **savings**: avoid the entire sandbox lifecycle (cents + 10–25 s) on bypassed events.

### Test contract

- `derive_sandbox_policy` is a pure function — table-driven unit tests cover (static × classifier_output × feature) cross-product.
- Property: `static == NO_SANDBOX → result == NO_SANDBOX` regardless of classifier.
- Property: `classifier_output is None → result == static` (fail-open).
- Property: review/fix never produce `NO_SANDBOX` from classifier output alone.
- Integration test: an event the classifier marks `intent=unclear` skips `factory()` / `clone()` entirely (dispatcher test asserts factory mock not called).

---

## The ref-resolution matrix

This is **the single highest-risk surface** in this slice. A bug here = agent looks at wrong code = wrong answer to user. Every cell must have a unit test.

| Trigger (`event.kind`) | Context | Workflow | Resolved `ref` | Resolved `diff_base` | `strategy` | Notes |
|---|---|---|---|---|---|---|
| `ISSUE_OPENED` | issue | triage | `default_branch` HEAD via `adapter.get_default_branch_sha(event)` | — | `SHALLOW` | Reproduce against current main. |
| `ISSUE_ASSIGNED` (assignee = bot) | issue | fix | `default_branch` HEAD | — | `SHALLOW` | Fix branches off latest main. |
| `ISSUE_LABELED` (label = `openbot:fix`) | issue | fix | `default_branch` HEAD | — | `SHALLOW` | Same as above. |
| `ISSUE_COMMENT_CREATED` on issue (no `pull_request` field) | issue | chat | `default_branch` HEAD | — | `BLOBLESS` | Chat about repo current state. |
| `ISSUE_COMMENT_CREATED` on PR (`payload.issue.pull_request` present) | PR | chat | `adapter.get_pull_request(event, pr_number).head.sha` | — | `BLOBLESS` | **Extra adapter call** — `issue_comment` payload doesn't carry head SHA. |
| `PR_OPENED` | PR | review | `payload.pull_request.head.sha` | `payload.pull_request.base.sha` | `SHALLOW_HISTORY` | First-time review; base = PR's merge base. |
| `PR_SYNCHRONIZE` | PR | review (incremental) | `payload.pull_request.head.sha` | `event.last_reviewed_sha` ?? `pull_request.base.sha` | `SHALLOW_HISTORY` | Diff is incremental; checkout is still head. |
| `PR_REOPENED` | PR | review | `payload.pull_request.head.sha` | `payload.pull_request.base.sha` | `SHALLOW_HISTORY` | Treat as fresh review. |
| `PR_REVIEW_COMMENT_CREATED` (inline) | PR | chat (inline) | `event.review_commit_id` | — | `BLOBLESS` | **Use comment's commit_id**, not head — otherwise line numbers drift if the file changed in later commits. |
| `PR_CLOSED` (merged) | PR | — | — | — | — | `SandboxPolicy.NO_SANDBOX` at router level. |
| `PR_LABELED` / `PR_UNLABELED` | PR | (cancel only) | — | — | — | `NO_SANDBOX` unless the label triggers a workflow re-run. |
| `ISSUE_LABELED` (label = `cancel-openbot`) | issue | — | — | — | — | `NO_SANDBOX`. |

**Reading the matrix**:
- "Context" determines whether `pr_number` or `issue_number` is non-null on `UnifiedEvent` (the existing distinguisher).
- "Resolved `ref`" is always a concrete SHA, never a branch name — branches are mutable and can change between webhook receipt and clone.
- "Resolved `diff_base`" is only populated for review workflows; other workflows ignore it.

### Resolution algorithm

```python
async def resolve_checkout(
    event: UnifiedEvent,
    workflow: Workflow,
    adapter: ChannelAdapterPort,
) -> CheckoutSpec:
    if event.clone_url is None:
        raise CheckoutResolutionError(f"event missing clone_url: kind={event.kind}")

    # --- Inline review comment: highest specificity, check first ---
    if event.kind is EventKind.PR_REVIEW_COMMENT_CREATED and event.review_commit_id:
        return CheckoutSpec(
            repo_url=event.clone_url,
            ref=event.review_commit_id,
            strategy=_strategy_for(workflow),
        )

    # --- PR-context dispatch ---
    if event.pr_number is not None:
        pr = await adapter.get_pull_request(event, event.pr_number)
        head_sha = pr["head"]["sha"]
        base_sha = pr["base"]["sha"]

        if workflow is Workflow.REVIEW:
            return CheckoutSpec(
                repo_url=event.clone_url,
                ref=head_sha,
                strategy=CloneStrategy.SHALLOW_HISTORY,
                diff_base=event.last_reviewed_sha or base_sha,
            )
        return CheckoutSpec(
            repo_url=event.clone_url,
            ref=head_sha,
            strategy=_strategy_for(workflow),
        )

    # --- Issue-context dispatch (issue webhooks + issue-thread comments) ---
    if event.issue_number is not None:
        default_sha = await adapter.get_default_branch_sha(event)
        return CheckoutSpec(
            repo_url=event.clone_url,
            ref=default_sha,
            strategy=_strategy_for(workflow),
        )

    raise CheckoutResolutionError(
        f"no resolution rule for kind={event.kind.value} workflow={workflow.value}"
    )


def _strategy_for(workflow: Workflow) -> CloneStrategy:
    return {
        Workflow.TRIAGE: CloneStrategy.SHALLOW,
        Workflow.REVIEW: CloneStrategy.SHALLOW_HISTORY,
        Workflow.FIX:    CloneStrategy.SHALLOW,
        Workflow.CHAT:   CloneStrategy.BLOBLESS,
    }[workflow]
```

**Property the tests must enforce**: every `EventKind × Workflow` combination either (a) returns a `CheckoutSpec`, (b) raises `CheckoutResolutionError`, or (c) is gated by `SandboxPolicy.NO_SANDBOX` at the router. There is no fourth state.

---

## Tool & capability matrix

Workflows differentiate at the responder layer by tools, prompt, and budget. The sandbox is the same shape for everyone.

| Responder | Tools | System prompt stance | Budget (PRD §6) |
|---|---|---|---|
| `DeepAgentsTriageResponder` | `read_file`, `list_files`, `grep_files`, `run` (allowlist: `python`, `node`, `pytest`, `jest`) | "Classify the issue. If reproducible, write a minimal repro and run it. No file writes other than `repro.{py,js}`." | $0.20 / 5 min |
| `DeepAgentsReviewResponder` | `read_file`, `list_files`, `grep_files`, `run` (allowlist: `ruff`, `mypy`, `eslint`, `tsc`, `pytest`) | "Review the diff between `diff_base` and `HEAD`. Emit findings with evidence (lint output, type error, test failure). Never modify code." | $0.50 / 5 min |
| `DeepAgentsFixResponder` (existing) | Full: `read_file`, `write_file`, `run` (no allowlist), `git_diff`, `commit_and_push` | Existing | $3.00 / 45 min |
| `DeepAgentsChatResponder` (modified) | `read_file`, `list_files`, `grep_files`, `run` (read-only allowlist) | **Reversed from current**: "You have a sandbox checkout at the workspace root. Use the file-reading tools to ground your answer — do not invent code you haven't read." | $0.30 / 3 min |

**Key invariant**: a workflow that should not modify code does not have `write_file` in its tool list. This is a physical safety boundary, not a prompt-level convention.

---

## Migration plan

This slice touches a lot of files but each change is small and additive. The migration is:

### 1. Add the new layer (no behavior change yet)

- Add `domain/checkout.py` (`CheckoutSpec`, `CloneStrategy`).
- Add `application/checkout_resolver.py` (`resolve_checkout`, `CheckoutResolutionError`).
- Add `application/sandbox_handle.py` (`SandboxedHandle`).
- Add `application/sandbox_policy.py` (`derive_sandbox_policy`).
- Add `SandboxPolicy` to `application/router.py`. Default every existing route to `REQUIRED` (which means: try to provision; if no factory → degrade).
- Add optional `sandbox_handle: SandboxedHandle | None` and `classifier_output: ClassifierOutput | None` fields to `PreflightContext`.
- Add `strategy: CloneStrategy = CloneStrategy.SHALLOW` param to `SandboxPort.clone`. Existing impls accept-and-ignore.
- Extend `UnifiedEvent` with `clone_url`, `review_commit_id`, `last_reviewed_sha` optional fields.
- Extend `ChannelAdapterPort` with `get_default_branch_sha` and `get_pull_request` (the latter may already exist for fix).

**Tests at this point**: 100% of `resolve_checkout` matrix; `derive_sandbox_policy` (static × classifier × feature) cross-product; round-trip `CheckoutSpec` serialization; `dataclasses.replace` correctness on `PreflightContext`.

### 2. Wire the provisioning step

- Modify `dispatcher.run_dispatch` to add the post-preflight provisioning block.
- **Move `classify_event` call from `decide.py` to dispatcher** so its output is available *before* the policy-merge step (today it runs in `decide.py` post-preflight but downstream of any sandbox decision we'd want to make).
- Call `derive_sandbox_policy(static=dispatch.sandbox_policy, classifier_output=…, feature=…)` and use the result to decide whether to provision.
- Pass `classifier_output` into the handler via `PreflightContext.classifier_output` so handlers (especially triage/chat bypass paths) can specialize their reply.
- Mark cancel / merged-PR / label-only routes as `SandboxPolicy.NO_SANDBOX` in the router.
- Keep `fix.py`'s internal clone in place TEMPORARILY (it will become a no-op because dispatcher pre-clones).

**Tests at this point**: dispatcher integration tests showing
- happy path: handler receives non-None `sandbox_handle` AND `classifier_output`;
- static bypass: cancel event → no clone, handler called with `sandbox_handle=None`;
- dynamic bypass: classifier returns `intent=unclear` → no clone, handler called with `sandbox_handle=None` and a populated `classifier_output`;
- fail-open: classifier raises → static policy honored, no `NO_SANDBOX` upgrade.

### 3. Migrate `fix.py`

- Delete `fix.py`'s `async with factory() as sandbox: ... sandbox.clone(...)` block.
- Use `ctx.sandbox_handle.sandbox` / `.token` / `.checkout` directly.
- Handle `ctx.sandbox_handle is None` → `_NO_SANDBOX` template (existing copy).

**Tests at this point**: fix E2E still green; ref now comes from `resolve_checkout` instead of `adapter.get_issue`.

### 4. Add new responders

- `DeepAgentsTriageResponder` + repro tools — triage use case now sandbox-grounded.
- `DeepAgentsReviewResponder` + lint/typecheck tools — review use case now sandbox-grounded; findings carry `evidence` field.
- `DeepAgentsChatResponder` modification — toolset and prompt change.

Each responder lands as its own commit. Stop here if the slice is getting too big — phases 1–3 are already valuable.

---

## Testing strategy

| Layer | Test style | What to cover |
|---|---|---|
| `CheckoutSpec` / `CloneStrategy` | Unit | Construction, frozen-ness, equality. |
| `resolve_checkout` | Unit, parametrized | Every (EventKind × Workflow) cell from the matrix. Use a fake `ChannelAdapterPort` returning canned PR/branch payloads. |
| `SandboxPort.clone(strategy=...)` | Unit per adapter | Fake ignores strategy. Daytona switches command between `--depth=1` / `--filter=blob:none` / `--depth=N`. |
| Dispatcher provisioning | Integration | Happy path: handler receives non-None `sandbox_handle`. Each degrade path: handler still called, `sandbox_handle is None`, no exception escapes. |
| Workflow handlers | Integration | Smoke for each (triage/review/fix/chat) that handler reads `ctx.sandbox_handle` correctly and degrades gracefully. |
| End-to-end | Existing E2E suites | All four workflows pass with the new entry point. |

**Property tests** (worth writing for `resolve_checkout`):

- For every `pr_number is not None` event, the returned `ref` equals the PR head SHA *except* when `kind is PR_REVIEW_COMMENT_CREATED`, in which case it equals `review_commit_id`.
- For every `issue_number is not None` event with no `pr_number`, the returned `ref` equals the default branch SHA returned by the adapter mock.
- `diff_base` is non-None iff `workflow is Workflow.REVIEW and pr_number is not None`.

---

## Open questions

1. **Cache layer ordering** — Spec deliberately scopes cache out. But for chat/triage to fit `$0.20–0.30` budgets, we need cache before responders go live. Should this slice land in two phases (entry-point first, then cache, then responders)? **Recommendation: yes, see "Implementation slicing" below.**

2. **`UnifiedEvent` field promotion** — Adding three optional fields is mostly mechanical, but it touches the event-ingest layer (slice A). Is there a cleaner path that keeps these in `event.raw`? **Tentative answer: no; typed fields buy us better static analysis on the resolver and prevent silent-None bugs.**

3. **`SandboxPolicy` granularity** — Currently binary (REQUIRED / NO_SANDBOX). Do we need a third state like `OPTIONAL_GROUNDING` for chat events that can degrade gracefully? **Tentative answer: no; graceful degrade is already encoded in "handler must accept `sandbox_handle is None`".**

4. **Multi-tenant warm pool** — Spec assumes per-event sandbox lifetime. If we later add per-repo warm pool (cache + reuse), does that violate any current invariant? **Tentative answer: no, because the cache lives inside `DaytonaSandboxAdapter.clone` — the dispatcher still calls `factory()` per event; whether the factory hands out a fresh sandbox or a hot one is opaque.**

5. **Token rotation for long-running workflows** — Fix can run 45 min, longer than GitHub installation token's 1h TTL but close. The current `DaytonaSandboxAdapter.commit_and_push` re-derives a fresh token at push time. Should we also rotate mid-run? **Recommendation: out of scope; revisit if fix begins to hit token-expiry errors empirically.**

6. **Classifier call site: dispatcher vs `decide.py` vs worker** — Today `classify_event` runs in `decide.py` after preflight but downstream of any sandbox decision. To gate sandbox provisioning on classifier output, we must run it *before* provisioning. Three options:
   - **(a) Move call to dispatcher** (between preflight and provisioning). Adds 0–5 s of LLM latency to the webhook-to-handler critical path on cache miss. **Recommended** — simplest data flow; classifier output ends up in `PreflightContext` anyway.
   - **(b) Run classifier inside preflight as a new middleware**. Cleanest layering, but middleware is supposed to be fast and side-effect-free; an LLM call doesn't fit.
   - **(c) Two-stage worker queue**: enqueue with `classifier_output=None`, classify in worker, re-enqueue. Over-engineered for a 1h-cached call.
   Going with (a) unless the latency profile changes.

7. **Should review ever bypass via classifier?** Currently the spec says no — review always grounds in-sandbox. But for ≤10-line trivial PRs the diff-only LLM verdict might be cost-equivalent. **Tentative answer: no for v0.1**; revisit when we have telemetry on small-PR cost vs grounded-PR cost.

---

## Implementation slicing

Recommended order (each numbered item ≈ 1 PR):

1. **Foundations**: `CheckoutSpec`, `CloneStrategy`, `resolve_checkout`, `SandboxedHandle`, `SandboxPolicy`, `UnifiedEvent` field extension, `SandboxPort.clone(strategy=)`. All additive; tests only.
2. **Dispatcher wiring**: post-preflight provisioning block, graceful-degrade paths, router `sandbox_policy` annotations. Existing handlers untouched.
3. **Cache layer (parallel)**: snapshot cache inside `DaytonaSandboxAdapter.clone`. Out of this spec but blocks 5/6/7 economically.
4. **`fix.py` migration**: drop internal clone, use `ctx.sandbox_handle`. No behavior change visible to GitHub.
5. **Triage repro responder**: new `DeepAgentsTriageResponder` + tools.
6. **Review grounded responder**: new `DeepAgentsReviewResponder` + tools + finding `evidence` field.
7. **Chat code-grounding**: modify `DeepAgentsChatResponder` toolset + system prompt reversal.

Items 1–4 can ship without touching responders — they unify the entry surface. Items 5–7 then layer on the new capability per workflow.

---

## Non-goals (explicitly out)

- Snapshot cache implementation (separate spec).
- Sparse-checkout / monorepo support (`sparse_paths` field reserved, not used).
- Cross-event sandbox reuse / warm pool (separate spec when needed).
- Plugin workflow support (v0.2).
- Linear / Slack channel adapters (PRD §9, post-v0.1).
- Auto-fix loop iteration on review findings (separate product feature).

---

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| `resolve_checkout` has a wrong cell → agent reads wrong code | **High** (silent wrong answers) | Exhaustive matrix tests; property tests; PR review focused on the matrix table. |
| Adding clone cost to every event blows past per-workflow budgets | High (chat/triage especially) | Cache layer (item 3) lands before responder migrations (items 5–7). |
| `UnifiedEvent` field addition breaks downstream consumers | Medium | All new fields are `Optional[...]` with `None` default. Ingest layer change is single-PR. |
| `SandboxPolicy.NO_SANDBOX` set incorrectly → sandbox provisioned for events that don't need it | Low (cost-only, no correctness impact) | Router-level decision; easy to audit and adjust. |
| Token now flows through more sandbox lifetimes | Low | Existing `_redact_tokens` + `_inject_token` already cover the surface; threat model unchanged. |
| Graceful-degrade contract gets bypassed by a handler that raises | Medium | Dispatcher already wraps handler call in `try/except` and logs; reinforce in handler-author docstrings. |
| Classifier false-positive `NO_SANDBOX` → legitimate event silently dropped (e.g., real bug report scored as `looks_like_spam`) | **High** (user-visible "OpenBot ignored my issue") | (a) Eval suite specifically targeting bypass-vs-engage decisions on the classifier — gated in CI. (b) Bypass paths still post a brief reply ("Could you share reproduction steps?") instead of silent drop, so user knows the bot saw the event. (c) `looks_like_spam` requires conjunction with `type ∈ {spam, question}` — single-signal bypass is avoided. |
| Classifier false-negative (spam classified as bug) → wastes a sandbox provision | Low (cost-only) | Bounded by existing per-task budgets + Redis cache amortization; net cost ≤ 1 sandbox per unique spam body per hour. |
| Classifier outage causes regression to "everything gets a sandbox" | Low | Fail-open is intentional; this is the pre-classifier baseline behavior. Add Prometheus counter for classifier-error rate; alert if > 5%. |
| Dispatcher now runs classifier *before* sandbox provisioning, adding 1–2 s on cache miss to every event's webhook ack time | Low | GitHub webhook timeout is 10 s; we ack with 202 before the classifier call. The added latency is in the worker-side execute path, not the public-facing ack. |

---

## Acceptance checklist

Before any of this lands as `feat/unified-sandbox-entry`:

- [ ] Spec reviewed by maintainer.
- [ ] Implementation plan written (`docs/superpowers/plans/2026-05-21-unified-sandbox-entry-plan.md`).
- [ ] Ref-resolution matrix tests pass for every cell.
- [ ] `derive_sandbox_policy` (static × classifier × feature) cross-product tested.
- [ ] Classifier-call relocation from `decide.py` → dispatcher does not break existing `stages_to_run` consumers in the worker.
- [ ] Eval suite added for classifier bypass-vs-engage decisions (gates against false-positive `NO_SANDBOX`).
- [ ] Cache layer slice (item 3) tracked separately if not in this PR.
- [ ] PRD doesn't need updates (this is implementation-layer; PRD already says "fix uses sandbox, triage may reproduce in sandbox").

---

## Appendix — alignment with existing code

- Slice A (`PreflightContext`, frozen + middleware chain): preserved. New `sandbox_handle` field is optional and set only by dispatcher via `dataclasses.replace`.
- Slice B (PR Review API): unaffected — review responder will continue to write structured findings.
- Slice C (fix workflow): `fix.py` becomes thinner; `SandboxPort` keeps its shape.
- Slice F (incremental review, `last_reviewed_sha`): consumed by `resolve_checkout`'s `diff_base` field; no new schema work.
- Slice D (`dispatcher/classifier.py` + `decide.py` integration): the classifier itself is unchanged; only its **call site** moves up from `decide.py` to `dispatcher.run_dispatch` so its output can gate provisioning. `stages_from_classifier` and `TaskSpec.classifier_output` serialization are preserved.
- Evals refactor: unaffected — `evals/sandboxes/` lives behind a separate Protocol per PRD §3 locked-boundary. A new `evals/triage_classifier/` suite will gate the false-positive `NO_SANDBOX` risk.
