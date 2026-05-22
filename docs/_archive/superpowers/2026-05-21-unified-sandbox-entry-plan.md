# Unified Sandbox Entry — Implementation plan (parts 1–7)

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Source spec:** [`docs/superpowers/specs/2026-05-21-unified-sandbox-entry-design.md`](../specs/2026-05-21-unified-sandbox-entry-design.md)
**Branch (proposed):** `feat/unified-sandbox-entry`
**Goal:** Move sandbox provisioning from `use_cases/fix.py` up to `dispatcher.run_dispatch`, gated by an OR-merge of static `SandboxPolicy` and the existing LLM intent classifier. Every workflow (triage/review/fix/chat) becomes sandbox-capable, with workflow-specific tool & prompt differentiation at the responder layer.

**Tech stack constraints (do not violate):**
- Python 3.12, pytest, `make check` green at every commit.
- No `--no-verify` on pre-commit hooks.
- Sandbox boundary is `SandboxPort` protocol in `openbot/application/ports/sandbox.py` (production) — distinct from `evals/sandboxes/factory.py` `SandboxBackend`. Do **not** cross-import.
- Observability = LangSmith; no Langfuse.
- v0.1 channel = GitHub only.
- Frozen dataclasses everywhere in `domain/` and `application/` value objects; mutate via `dataclasses.replace`.

---

## Status checkpoint (2026-05-21, branch `feat/unified-sandbox-entry`)

| Part | Status | Commits |
|---|---|---|
| 1 — Foundations | ✅ landed | `f4d2b7d` `e2c8166` `9bf8961` `dbf242d` `e667f26` `8c5fe8a` `0bbc4e7` `2ad4ff0` `2d84b7b` |
| 2.1 — Classifier relocation | ✅ landed | `4251a6b` |
| 2.2 — OR-merge policy + provisioning | ✅ landed | `db93427` |
| 2.3 — Bypass observability counters | ✅ landed | `1be5dbc` |
| 3 — `fix.py` migration | ✅ landed | `3a0ab50` |
| 4 — Snapshot cache | 📝 spec (`f1a142d`) + plan (`22749dd`) drafted; impl pending | [spec](../specs/2026-05-21-sandbox-snapshot-cache-design.md) / [plan](./2026-05-21-sandbox-snapshot-cache-plan.md) |
| 5 — Triage repro responder | ⏳ pending | — |
| 6 — Review grounded responder | ⏳ pending | — |
| 7 — Chat code-grounding responder | ⏳ pending | — |

**Tests at checkpoint:** 1097 passing, hexagonal contract held across all 14 commits, no `--no-verify`. See "Retro" section at the bottom of this file for what we learned in Parts 1–3.

---

## Per-PR slicing

Each numbered part below ≈ one PR. They land in order; later parts may merge later if responder development needs more iteration.

| Part | Theme | Touches responders? | Touches dispatcher? | Behavior change? |
|---|---|---|---|---|
| 1 | Foundations (data types + pure functions) | No | No | None — additive only |
| 2 | Dispatcher wiring + classifier relocation | No | Yes | Yes — gate provisioning on classifier |
| 3 | `fix.py` migration | No | No | None — internal refactor |
| 4 | Snapshot cache adapter (out-of-spec but blocks 5–7) | No | No | Cost-only |
| 5 | Triage repro responder | Yes | No | Yes — triage now enters sandbox |
| 6 | Review grounded responder | Yes | No | Yes — review now enters sandbox |
| 7 | Chat code-grounding responder | Yes | No | Yes — chat now enters sandbox |

Parts 1–3 are mandatory for the unification; 4 is a cost prerequisite for 5–7; 5–7 are independent capability adds.

---

## Type & symbol contract (locked across the plan)

Method/property names in later parts must match this table exactly. If you discover a mismatch during implementation, **fix the earliest part and re-run its tests** instead of papering over downstream.

| Symbol | Defined in | Used by |
|---|---|---|
| `CloneStrategy` (StrEnum: BLOBLESS, SHALLOW, SHALLOW_HISTORY, FULL) | `openbot/domain/checkout.py` (Part 1) | All clones; `SandboxPort.clone(strategy=)` |
| `CheckoutSpec` (frozen dataclass) | `openbot/domain/checkout.py` (Part 1) | Resolver, dispatcher, handlers |
| `CheckoutResolutionError` (Exception) | `openbot/domain/checkout.py` (Part 1) | Resolver raises; dispatcher catches → degrade |
| `resolve_checkout(event, workflow, adapter) -> CheckoutSpec` | `openbot/application/checkout_resolver.py` (Part 1) | Dispatcher (Part 2) |
| `SandboxedHandle` (frozen dataclass: sandbox, checkout, token) | `openbot/application/sandbox_handle.py` (Part 1) | Dispatcher (Part 2), every handler (Parts 3, 5–7) |
| `SandboxPolicy` (StrEnum: REQUIRED, NO_SANDBOX) | `openbot/application/router.py` (Part 1, modify) | Router rows, `derive_sandbox_policy` |
| `derive_sandbox_policy(*, static, classifier_output, feature) -> SandboxPolicy` | `openbot/application/sandbox_policy.py` (Part 1) | Dispatcher (Part 2) |
| `PreflightContext.sandbox_handle: SandboxedHandle \| None` | `openbot/application/middleware/preflight.py` (Part 1) | All handlers |
| `PreflightContext.classifier_output: ClassifierOutput \| None` | `openbot/application/middleware/preflight.py` (Part 1) | All handlers (bypass-aware reply) |
| `ChannelAdapterPort.get_default_branch_sha(event) -> str` | `openbot/application/ports/channel_adapter.py` (Part 1) | Resolver (Part 1), GitHub adapter (Part 1) |
| `ChannelAdapterPort.get_pull_request(event, pr_number) -> dict` | already exists from Slice C.4 — verify shape | Resolver (Part 1) |
| `SandboxPort.clone(..., strategy: CloneStrategy = SHALLOW)` | `openbot/application/ports/sandbox.py` (Part 1) | Adapters (Part 1), dispatcher (Part 2) |
| `UnifiedEvent.clone_url: str \| None` | `openbot/domain/events.py` (Part 1) | Resolver |
| `UnifiedEvent.review_commit_id: str \| None` | `openbot/domain/events.py` (Part 1) | Resolver (inline review comments) |
| `UnifiedEvent.last_reviewed_sha: str \| None` | `openbot/domain/events.py` (Part 1) | Resolver `diff_base` for review |

---

## Part 1 — Foundations (data types + pure functions) ✅

**Goal:** All new value types, ports, and pure functions land with full test coverage. Zero behavior change visible to GitHub. Existing tests still pass.

**Files:**

| Path | Action |
|---|---|
| `openbot/domain/checkout.py` | NEW |
| `openbot/application/checkout_resolver.py` | NEW |
| `openbot/application/sandbox_handle.py` | NEW |
| `openbot/application/sandbox_policy.py` | NEW |
| `openbot/application/router.py` | MODIFY (+ `SandboxPolicy`, + `Dispatch.sandbox_policy` field) |
| `openbot/application/middleware/preflight.py` | MODIFY (+ `sandbox_handle`, + `classifier_output` fields) |
| `openbot/application/ports/sandbox.py` | MODIFY (`clone(..., strategy=)`) |
| `openbot/application/ports/channel_adapter.py` | MODIFY (+ `get_default_branch_sha`) |
| `openbot/infrastructure/sandboxes/fake.py` | MODIFY (accept-and-ignore `strategy`) |
| `openbot/infrastructure/sandboxes/daytona.py` | MODIFY (switch on `strategy`) |
| `openbot/infrastructure/adapters/github.py` | MODIFY (+ `get_default_branch_sha` impl) |
| `openbot/domain/events.py` | MODIFY (+ 3 optional fields + ingest extraction) |
| `openbot/eps/github/webhook.py` (or wherever ingest is) | MODIFY (populate new fields) |
| `tests/domain/test_checkout.py` | NEW |
| `tests/application/test_checkout_resolver.py` | NEW |
| `tests/application/test_sandbox_policy.py` | NEW |
| `tests/infrastructure/sandboxes/test_daytona.py` | MODIFY (+ strategy tests) |
| `tests/_fakes/channel_adapter.py` | MODIFY (+ `get_default_branch_sha` stub) |

### Task 1.1: `CheckoutSpec` + `CloneStrategy`

- [ ] **Write failing test** `tests/domain/test_checkout.py`:
  - `test_checkout_spec_is_frozen` — assigning to a field raises `FrozenInstanceError`.
  - `test_clone_strategy_values` — enum has exactly BLOBLESS/SHALLOW/SHALLOW_HISTORY/FULL.
  - `test_default_strategy_is_shallow` — `CheckoutSpec(repo_url=..., ref=...).strategy is CloneStrategy.SHALLOW`.
  - `test_equality_and_hash` — two instances with same fields are equal and hashable.
- [ ] **Implement** `openbot/domain/checkout.py`:
  - `class CloneStrategy(StrEnum)` with the 4 values.
  - `@dataclass(frozen=True, slots=True) class CheckoutSpec` with fields: `repo_url`, `ref`, `strategy: CloneStrategy = SHALLOW`, `diff_base: str | None = None`, `sparse_paths: tuple[str, ...] = ()`.
  - `class CheckoutResolutionError(Exception): pass`.
- [ ] Run `make check`. Commit: `feat(domain): add CheckoutSpec + CloneStrategy`.

### Task 1.2: `SandboxedHandle`

- [ ] **Write failing test** `tests/application/test_sandbox_handle.py`:
  - `test_sandboxed_handle_holds_components` — construct with mock sandbox + `CheckoutSpec(...)` + token; assert fields.
  - `test_sandboxed_handle_is_frozen`.
- [ ] **Implement** `openbot/application/sandbox_handle.py`:
  - `@dataclass(frozen=True, slots=True) class SandboxedHandle` with `sandbox: SandboxPort`, `checkout: CheckoutSpec`, `token: str`.
- [ ] Run `make check`. Commit: `feat(application): add SandboxedHandle`.

### Task 1.3: `SandboxPolicy` + `Dispatch.sandbox_policy`

- [ ] **Write failing test** in `tests/application/test_router.py` (extend existing):
  - `test_dispatch_default_sandbox_policy_is_required` — `Dispatch(...).sandbox_policy is SandboxPolicy.REQUIRED`.
  - `test_dispatch_accepts_no_sandbox_policy` — explicit `SandboxPolicy.NO_SANDBOX` round-trips.
- [ ] **Implement** in `openbot/application/router.py`:
  - Add `class SandboxPolicy(StrEnum)` with `REQUIRED = "required"` and `NO_SANDBOX = "no_sandbox"`.
  - Add `sandbox_policy: SandboxPolicy = SandboxPolicy.REQUIRED` field to `Dispatch`.
- [ ] Audit existing router rows; mark these as `SandboxPolicy.NO_SANDBOX`:
  - `PR_CLOSED` when `merged=True`.
  - `ISSUE_LABELED` / `PR_LABELED` when label is `cancel-openbot`.
  - Any direct-action route in `RULES_BY_FEATURE` that does not invoke a responder.
- [ ] Run `make check`. Commit: `feat(application): add SandboxPolicy + tag bypass routes`.

### Task 1.4: `derive_sandbox_policy` (OR-merge of static + classifier)

- [ ] **Write failing test** `tests/application/test_sandbox_policy.py` — parametrized table for the full cross-product:

  | static | classifier_output | feature | expected |
  |---|---|---|---|
  | NO_SANDBOX | (any) | (any) | NO_SANDBOX |
  | REQUIRED | `None` | (any) | REQUIRED (fail-open) |
  | REQUIRED | `TriageClassifierOutput(looks_like_spam=True, ...)` | TRIAGE | NO_SANDBOX |
  | REQUIRED | `TriageClassifierOutput(type="question", has_reproduction_info=False)` | TRIAGE | NO_SANDBOX |
  | REQUIRED | `TriageClassifierOutput(type="bug", has_reproduction_info=True)` | TRIAGE | REQUIRED |
  | REQUIRED | `ChatClassifierOutput(intent="unclear")` | CHAT | NO_SANDBOX |
  | REQUIRED | `ChatClassifierOutput(intent="out_of_scope")` | CHAT | NO_SANDBOX |
  | REQUIRED | `ChatClassifierOutput(intent="readonly_qa")` | CHAT | REQUIRED |
  | REQUIRED | `ReviewClassifierOutput(...)` | REVIEW | REQUIRED (never bypasses on classifier) |
  | REQUIRED | (any) | FIX | REQUIRED (no classifier for fix) |

- [ ] **Implement** `openbot/application/sandbox_policy.py` per spec § "Intent classification integration → The merge function".
- [ ] Run `make check`. Commit: `feat(application): derive_sandbox_policy OR-merges static + classifier`.

### Task 1.5: `PreflightContext` extension

- [ ] **Write failing test** in `tests/application/middleware/test_preflight.py`:
  - `test_preflight_context_sandbox_handle_default_is_none`.
  - `test_preflight_context_classifier_output_default_is_none`.
  - `test_replace_sandbox_handle_preserves_other_fields` — verify `dataclasses.replace` immutability discipline.
- [ ] **Implement** in `openbot/application/middleware/preflight.py`:
  - Add `sandbox_handle: SandboxedHandle | None = None` field.
  - Add `classifier_output: ClassifierOutput | None = None` field (where `ClassifierOutput` is a union from `openbot.dispatcher.classifier`).
- [ ] Run `make check`. Commit: `feat(preflight): carry SandboxedHandle + ClassifierOutput on context`.

### Task 1.6: `SandboxPort.clone(strategy=...)`

- [ ] **Write failing test** `tests/infrastructure/sandboxes/test_fake.py`:
  - `test_fake_clone_accepts_strategy` — pass each `CloneStrategy` value; assert no error.
- [ ] **Modify** `openbot/application/ports/sandbox.py`:
  - Update `clone` Protocol signature: `async def clone(self, repo_url: str, ref: str, token: str, *, strategy: CloneStrategy = CloneStrategy.SHALLOW) -> None: ...`.
- [ ] **Modify** `openbot/infrastructure/sandboxes/fake.py`: accept and ignore `strategy` (record it on the fake for assertion).
- [ ] **Modify** `openbot/infrastructure/sandboxes/daytona.py`:
  - Build the git command from a `_clone_args(strategy)` helper.
  - `SHALLOW` → `--depth=1 --branch={ref}` (current behavior).
  - `SHALLOW_HISTORY` → `--depth=50` (reasonable diff window; const).
  - `BLOBLESS` → `--filter=blob:none --no-checkout` then `git checkout {ref}`.
  - `FULL` → no extra flags.
- [ ] Add `tests/infrastructure/sandboxes/test_daytona.py` cases that assert command shape per strategy (mock the SDK).
- [ ] Run `make check`. Commit: `feat(sandbox): SandboxPort.clone accepts CloneStrategy`.

### Task 1.7: `ChannelAdapterPort.get_default_branch_sha`

- [ ] **Write failing test** `tests/infrastructure/adapters/test_github.py`:
  - Mock GitHub `GET /repos/{owner}/{repo}` returning `{"default_branch": "main"}`, then `GET /repos/{owner}/{repo}/branches/main` returning a SHA.
  - Assert `adapter.get_default_branch_sha(event)` returns that SHA.
- [ ] **Modify** `openbot/application/ports/channel_adapter.py`:
  - Add `async def get_default_branch_sha(self, event: UnifiedEvent) -> str: ...` to the Protocol.
- [ ] **Implement** in `openbot/infrastructure/adapters/github.py`.
- [ ] **Update** `tests/_fakes/channel_adapter.py` with a `get_default_branch_sha` stub returning a constant SHA.
- [ ] Run `make check`. Commit: `feat(github): get_default_branch_sha for resolver`.

### Task 1.8: `UnifiedEvent` field promotion + ingest

- [ ] **Write failing test** `tests/domain/test_events.py` (extend existing):
  - `test_unified_event_clone_url_default_is_none`.
  - `test_unified_event_review_commit_id_default_is_none`.
  - `test_unified_event_last_reviewed_sha_default_is_none`.
- [ ] **Write failing test** for ingest:
  - PR webhook payload → `event.clone_url == payload["repository"]["clone_url"]`.
  - Inline review comment payload → `event.review_commit_id == payload["comment"]["commit_id"]`.
- [ ] **Modify** `openbot/domain/events.py`: add 3 `Optional[str] = None` fields to `UnifiedEvent`.
- [ ] **Modify** ingest (`openbot/eps/github/webhook.py` or wherever `UnifiedEvent` is constructed):
  - Extract `clone_url` from `payload.repository.clone_url`.
  - Extract `review_commit_id` from `payload.comment.commit_id` when EventKind is `PR_REVIEW_COMMENT_CREATED`.
  - Pull `last_reviewed_sha` from DB during dispatch enqueue (already done in Slice F — just thread it through).
- [ ] Run `make check`. Commit: `feat(events): promote clone_url, review_commit_id, last_reviewed_sha to typed fields`.

### Task 1.9: `resolve_checkout` (pure function + adapter calls)

- [ ] **Write failing test** `tests/application/test_checkout_resolver.py` — table-driven, one parametrize per row of the spec's ref-resolution matrix (11 cells minimum):
  - For each cell: build a fake `UnifiedEvent` with the right `kind`/`pr_number`/`issue_number`/`review_commit_id`; mock the `ChannelAdapterPort`; assert returned `CheckoutSpec.ref`, `diff_base`, `strategy`.
  - Edge case: `event.clone_url is None` → raises `CheckoutResolutionError`.
  - Edge case: `EventKind.UNKNOWN` with no PR/issue context → raises `CheckoutResolutionError`.
- [ ] **Implement** `openbot/application/checkout_resolver.py` per spec § "Resolution algorithm".
- [ ] Property test (use `hypothesis` if already in deps; otherwise parametrize):
  - For every `pr_number is not None` event, `ref == pr.head.sha` unless `kind == PR_REVIEW_COMMENT_CREATED`.
  - For every `issue_number is not None` event with no `pr_number`, `ref == default_branch_sha`.
  - `diff_base` is non-None iff `workflow == REVIEW and pr_number is not None`.
- [ ] Run `make check`. Commit: `feat(application): resolve_checkout matrix`.

**Part 1 acceptance:** all new modules have ≥ 95% line coverage; `make check` green; no behavior visible to GitHub yet because dispatcher hasn't been wired.

---

## Part 2 — Dispatcher wiring + classifier relocation ✅

**Goal:** `dispatcher.run_dispatch` provisions the sandbox after preflight, gated by the OR-merged policy. Existing fix path keeps working (it still has its own internal clone — that's removed in Part 3).

**Files:**

| Path | Action |
|---|---|
| `openbot/application/dispatcher.py` | MODIFY (insert classifier call + policy gate + provisioning) |
| `openbot/dispatcher/decide.py` | MODIFY (remove classifier call from here — moved up) |
| `openbot/dispatcher/classifier.py` | (no change — only call site moves) |
| `tests/application/test_dispatcher.py` | NEW or MODIFY — provisioning integration tests |
| `tests/dispatcher/test_decide.py` | MODIFY — drop classifier assertions, keep stages_to_run plumbing |

### Task 2.1: Move `classify_event` call from `decide.py` to `dispatcher.run_dispatch`

- [ ] Read `openbot/dispatcher/decide.py` around line 130–220 (where it calls `classify_event` post-preflight).
- [ ] **Write failing test** in `tests/application/test_dispatcher.py`:
  - `test_dispatcher_calls_classifier_after_preflight` — preflight passes → classifier called once with `(event, feature)`.
  - `test_dispatcher_skips_classifier_when_static_no_sandbox` — `dispatch.sandbox_policy == NO_SANDBOX` short-circuits before classifier (saves an LLM call).
- [ ] **Move** the `classify_event(...)` invocation from `decide.py` into `dispatcher.run_dispatch`, between the preflight return-value check and the handler call.
- [ ] Thread the result into `PreflightContext.classifier_output` via `dataclasses.replace`.
- [ ] Confirm `decide.py` still passes `classifier_output` through to `TaskSpec` (`stages_from_classifier` etc. unchanged) — it just now consumes the result from context instead of computing it.
- [ ] Run `make check`. Commit: `refactor(dispatcher): move classify_event to dispatcher layer`.

### Task 2.2: OR-merge policy + provisioning block

- [ ] **Write failing test** `tests/application/test_dispatcher.py`:
  - `test_dispatcher_provisions_sandbox_on_required_policy` — handler called with `ctx.sandbox_handle is not None`.
  - `test_dispatcher_skips_provisioning_on_static_no_sandbox` — handler called with `ctx.sandbox_handle is None`; factory mock not called.
  - `test_dispatcher_skips_provisioning_on_classifier_unclear_chat` — handler called with `ctx.sandbox_handle is None` AND `ctx.classifier_output.intent == "unclear"`.
  - `test_dispatcher_degrades_gracefully_on_clone_failure` — `sandbox.clone` raises → handler called with `ctx.sandbox_handle is None`; no exception escapes.
  - `test_dispatcher_degrades_gracefully_on_factory_none` — `ctx.sandbox_factory is None` → handler called with `ctx.sandbox_handle is None`.
  - `test_dispatcher_degrades_gracefully_on_resolver_error` — `resolve_checkout` raises `CheckoutResolutionError` → handler called with `ctx.sandbox_handle is None`.
- [ ] **Implement** in `openbot/application/dispatcher.py`:
  ```python
  effective_policy = derive_sandbox_policy(
      static=dispatch.sandbox_policy,
      classifier_output=classifier_output,
      feature=dispatch.feature,
  )
  if effective_policy is SandboxPolicy.NO_SANDBOX or ctx.sandbox_factory is None:
      sandboxed_ctx = dataclasses.replace(ctx, classifier_output=classifier_output)
      await dispatch.handler(sandboxed_ctx)
      return

  try:
      checkout = await resolve_checkout(event, dispatch.feature, adapter)
      token = await adapter.get_installation_token(event)
  except (CheckoutResolutionError, AdapterError) as exc:
      logger.warning("sandbox provisioning skipped: %s", exc)
      sandboxed_ctx = dataclasses.replace(ctx, classifier_output=classifier_output)
      await dispatch.handler(sandboxed_ctx)
      return

  try:
      async with ctx.sandbox_factory() as sandbox:
          try:
              await sandbox.clone(
                  repo_url=checkout.repo_url,
                  ref=checkout.ref,
                  token=token,
                  strategy=checkout.strategy,
              )
          except SandboxError as exc:
              logger.warning("clone failed: %s", exc)
              sandboxed_ctx = dataclasses.replace(ctx, classifier_output=classifier_output)
              await dispatch.handler(sandboxed_ctx)
              return
          sandboxed_ctx = dataclasses.replace(
              ctx,
              classifier_output=classifier_output,
              sandbox_handle=SandboxedHandle(sandbox, checkout, token),
          )
          await dispatch.handler(sandboxed_ctx)
  except SandboxError as exc:
      logger.exception("sandbox factory failed: %s", exc)
      # last resort: still call handler so it can post a degrade reply
      sandboxed_ctx = dataclasses.replace(ctx, classifier_output=classifier_output)
      await dispatch.handler(sandboxed_ctx)
  ```
- [ ] Replicate the same block in `execute_handler` (worker-side entry point).
- [ ] Run `make check`. Commit: `feat(dispatcher): provision sandbox at entry, OR-merge classifier policy`.

### Task 2.3: Audit logs + metrics for bypass paths

- [ ] **Write failing test** assert log line with structured fields `{event=delivery_id, feature, sandbox_policy=REQUIRED|NO_SANDBOX, bypass_source=static|classifier|degrade}`.
- [ ] **Add** Prometheus counter `openbot_dispatch_sandbox_total{feature,policy,bypass_source}`.
- [ ] **Add** counter `openbot_classifier_error_total{feature}` — fires on classifier exception (catalogues fail-open occurrences).
- [ ] Run `make check`. Commit: `feat(observability): metrics for sandbox bypass paths`.

**Part 2 acceptance:** existing fix E2E test still green (fix handler is now called with a non-None `sandbox_handle` but its internal clone is still in place); all new dispatcher tests pass; classifier-fail-open verified.

---

## Part 3 — `fix.py` migration ✅

**Goal:** Drop `fix.py`'s internal `factory() → clone()` block; use `ctx.sandbox_handle` instead. Preserves all existing fallback copy.

**Files:**

| Path | Action |
|---|---|
| `openbot/application/use_cases/fix.py` | MODIFY (remove ~30 lines of provisioning) |
| `tests/application/use_cases/test_fix.py` | MODIFY (use `SandboxedHandle` directly in ctx) |
| `tests/e2e/test_spec_demos.py` | (verify demo 08 still green) |

### Task 3.1: Replace internal provisioning with `ctx.sandbox_handle`

- [ ] **Write failing test** `tests/application/use_cases/test_fix.py`:
  - `test_fix_uses_sandbox_handle_from_context` — handler given `ctx.sandbox_handle = SandboxedHandle(...)` runs the responder against it; never opens a new sandbox.
  - `test_fix_degrades_when_sandbox_handle_none` — `ctx.sandbox_handle is None` → posts `_NO_SANDBOX` reply.
- [ ] **Modify** `openbot/application/use_cases/fix.py`:
  - Delete the `async with ctx.sandbox_factory() as sandbox:` block.
  - Replace with:
    ```python
    if ctx.sandbox_handle is None:
        await _post_no_sandbox_reply(...)
        return
    sandbox = ctx.sandbox_handle.sandbox
    token = ctx.sandbox_handle.token
    checkout = ctx.sandbox_handle.checkout
    # rest of fix loop unchanged — responder, commit_and_push, PR open
    ```
  - Keep all error templates (`_NO_SANDBOX`, `_CLONE_FAIL`, `_PUSH_FAIL`, etc.) — they're still relevant for downstream failures.
- [ ] Update `audit_lifecycle` so it knows whether the sandbox was pre-provisioned or degraded.
- [ ] Run `make check`. Run E2E demo 08. Commit: `refactor(fix): consume ctx.sandbox_handle instead of internal clone`.

**Part 3 acceptance:** fix E2E green; no double-clone; dispatcher integration test confirms only one factory open per event.

---

## Part 4 — Snapshot cache adapter *(separate spec needed; out-of-scope for this plan but blocks Parts 5–7 economically)*

**Why here:** Parts 5–7 expand sandbox use from 1 workflow to 4. Without a cache, chat events that should cost $0.05 would pay full Daytona cold-start (~10–15 s + $0.01–0.05) per webhook. This part exists as a placeholder for the cache work.

**Out of this plan.** Recommended follow-up:
- Spec: `docs/superpowers/specs/2026-05-XX-sandbox-snapshot-cache.md`.
- Plan: separate, after Part 3 lands and we have telemetry on current cold-start cost.
- Acceptance criterion: chat workflow on a cached repo+SHA returns `sandbox_handle` ready in < 1 s P95.

**If Parts 5–7 land before this cache:** gate them behind an env flag (`OPENBOT_GROUNDED_RESPONDERS=false`) defaulting to off, so per-budget regression is contained to opt-in deployments.

---

## Part 5 — Triage repro responder

**Goal:** Triage workflow gets a grounded repro path. When classifier says `type=bug + has_reproduction_info=True`, the responder uses tools to write a `repro.py` (or `.js`) and run it. Findings cite real exception output, not hallucinated paths.

**Files:**

| Path | Action |
|---|---|
| `openbot/infrastructure/agents/_triage_tools.py` | NEW |
| `openbot/infrastructure/agents/deepagents_triage.py` | NEW |
| `openbot/infrastructure/agents/__init__.py` | MODIFY (+ export) |
| `openbot/application/use_cases/triage.py` | MODIFY (use new responder when classifier signals repro) |
| `tests/infrastructure/agents/test_triage_tools.py` | NEW |
| `tests/infrastructure/agents/test_deepagents_triage.py` | NEW |

### Task 5.1: `_triage_tools.py` (read-only + bounded run)

- [ ] **Write failing test**: each tool — `read_file`, `list_files`, `grep_files`, `run` — accepts the expected args, calls the sandbox, redacts tokens in output.
- [ ] **Implement** `_triage_tools.py`:
  - Reuse `_fix_tools.py`'s `read_file` / `list_files` / `grep_files` (extract to a shared `_sandbox_tools.py` if not already shared).
  - `run` tool with allowlist: `python`, `python3`, `node`, `pytest`, `jest` (deny everything else with a clear error message).
  - Per-tool budget: 10 calls default.
- [ ] Commit: `feat(agents): triage tools (read-only + sandboxed repro)`.

### Task 5.2: `DeepAgentsTriageResponder`

- [ ] **Write failing test**:
  - Given a bug report payload with classifier `has_reproduction_info=True` and a sandbox containing a real Python repo, responder writes `repro.py`, runs it, returns `TriageOutcome(...)` whose `evidence` includes the exception output.
  - Given a sandbox-less context (`ctx.sandbox_handle is None`), responder runs in "knowledge-only" mode (no tools) — preserves backward compatibility.
- [ ] **Implement** `openbot/infrastructure/agents/deepagents_triage.py`:
  - `DeepAgentsTriageResponder.triage_for_event(event, *, sandbox, classifier_output) -> TriageOutcome`.
  - System prompt per spec § "Tool & capability matrix" row 1.
  - Budget $0.20 / 5 min.
- [ ] Commit: `feat(agents): DeepAgentsTriageResponder`.

### Task 5.3: Wire into `use_cases/triage.py`

- [ ] **Write failing test**:
  - Bug event with sandbox → responder called with sandbox; resulting comment carries repro evidence.
  - Spam/unclear event (bypass path) → responder NOT called; existing label-only path runs.
- [ ] **Modify** `openbot/application/use_cases/triage.py` to select responder based on `ctx.sandbox_handle is None` vs not.
- [ ] Commit: `feat(triage): grounded repro responder when sandbox available`.

**Part 5 acceptance:** triage E2E suite green; new "bug-with-repro" demo case lands in `tests/e2e/test_spec_demos.py` proving the repro evidence shows up in the GitHub comment.

---

## Part 6 — Review grounded responder

**Goal:** Review workflow runs lint/typecheck/tests against the diff inside the sandbox. Each finding carries evidence (line + tool output). Replaces today's diff-only review.

**Files:**

| Path | Action |
|---|---|
| `openbot/infrastructure/agents/_review_tools.py` | NEW |
| `openbot/infrastructure/agents/deepagents_review.py` | NEW (or modify existing) |
| `openbot/domain/review.py` | MODIFY (Finding.evidence field — if not already in Slice B) |
| `openbot/infrastructure/agents/_review_schema.py` | MODIFY (pydantic Finding model + evidence) |
| `tests/infrastructure/agents/test_review_tools.py` | NEW |
| `tests/infrastructure/agents/test_deepagents_review.py` | NEW/MODIFY |

### Task 6.1: `_review_tools.py`

- [ ] Same shape as `_triage_tools.py` but with allowlist: `ruff`, `mypy`, `pyright`, `eslint`, `tsc`, `pytest`, `jest`.
- [ ] Add a `git_diff(base, head)` tool (read-only — uses `sandbox.git_diff` with `diff_base` from checkout).
- [ ] Commit: `feat(agents): review tools (lint/typecheck/test allowlist + git_diff)`.

### Task 6.2: `DeepAgentsReviewResponder` migration

- [ ] **Write failing test**: PR with a real type error → responder runs `mypy`, finding's `evidence` field contains the mypy output.
- [ ] **Modify** existing review responder OR add new one alongside, gated by env flag.
- [ ] Findings shape: `Finding(file, line, severity, message, evidence: str | None, category)`.
- [ ] Commit: `feat(agents): DeepAgentsReviewResponder grounded in sandbox`.

### Task 6.3: Update Findings → PR Review API bridge

- [ ] Ensure `evidence` field flows into the PR Review comment body (markdown code block).
- [ ] Update `findings_to_pr_review` (Slice B) accordingly.
- [ ] Commit: `feat(review): include evidence in PR review comments`.

**Part 6 acceptance:** review E2E demo with a deliberately-broken-mypy PR shows the mypy stderr in the resulting review comment.

---

## Part 7 — Chat code-grounding

**Goal:** Chat responder reads files from sandbox to ground its answers. Reverses the current "do not claim you inspected files" prompt.

**Files:**

| Path | Action |
|---|---|
| `openbot/infrastructure/agents/_chat_tools.py` | NEW (read-only subset of `_fix_tools`) |
| `openbot/infrastructure/agents/deepagents_chat.py` | MODIFY (tools + prompt) |
| `tests/infrastructure/agents/test_chat_tools.py` | NEW |
| `tests/infrastructure/agents/test_deepagents_chat.py` | MODIFY |

### Task 7.1: `_chat_tools.py`

- [ ] `read_file`, `list_files`, `grep_files` only — NO `run`, NO `write_file`.
- [ ] Commit: `feat(agents): chat tools (read-only)`.

### Task 7.2: Modify `DeepAgentsChatResponder`

- [ ] **Write failing test**:
  - Question "what does the foo function do?" → responder calls `grep_files` then `read_file`, answer contains real code lines (asserted via substring match on the actual file content from fake sandbox).
  - `ctx.sandbox_handle is None` (bypass path) → responder uses the OLD prompt (knowledge-only with the "do not claim" disclaimer).
- [ ] **Modify** `deepagents_chat.py`:
  - Two code paths in one responder: with-sandbox prompt vs without-sandbox prompt.
  - Tools attached only in the with-sandbox path.
  - `@lru_cache` key must include the with/without flag.
- [ ] Budget: $0.30 / 3 min.
- [ ] Commit: `feat(agents): DeepAgentsChatResponder grounds in sandbox when available`.

**Part 7 acceptance:** chat E2E demo: ask "what does X do?" on a real repo file → response cites the actual code (assertion: file content substring appears in the reply); ask "hello" → bypass path; `intent=unclear` clarification reply also works.

---

## Cross-cutting concerns

### Security

- [ ] Verify `_redact_tokens` still works post-migration — tokens flow through `SandboxedHandle.token` but never appear in handler-visible logs.
- [ ] All `clone()` paths inject token via `https://x-access-token:{token}@github.com/...` (existing `_inject_token`).
- [ ] No fallback to ssh:// (no leak surface for the bot's installation token).
- [ ] Add a regression test: assert that any string returned from a tool call passes through `_redact_tokens` before reaching the LLM context.

### Per-task budget enforcement (PRD §6)

- [ ] Each new responder must declare a budget; budget is enforced in the responder loop (existing pattern from `DeepAgentsFixResponder`).
- [ ] Add a counter `openbot_responder_budget_exhausted_total{feature}` so we can see budget hits in production.

### Documentation

- [ ] Update `CONTEXT.md` (or `docs/adr/`) with an ADR-style note: "Sandbox provisioning is dispatcher-level; OR-merge of router-static + classifier-dynamic policy."
- [ ] Update PRD §3 if/when it diverges (currently it just says "sandbox pluggable" — no divergence expected).
- [ ] Move this plan + spec to `docs/_archive/superpowers/` after final PR lands (per `CLAUDE.md` archive rule).

### Eval suite

- [ ] Add `evals/triage_classifier_bypass/` (or similar) — gates against the high-severity risk in spec: classifier false-positive `NO_SANDBOX`.
- [ ] Dataset: hand-curated mix of `(real bug, real spam, real question)` events.
- [ ] Metric: bypass-precision (∝ "of events the classifier said bypass, how many were truly non-productive?") + engagement-recall.
- [ ] Threshold: bypass-precision ≥ 0.95, engagement-recall ≥ 0.99 (we'd rather pay for a few spam sandboxes than drop a real bug).

---

## Risk-aware ordering

If any part is delayed:
- Parts 1–2 must land together to maintain coherence (Part 1 alone is dead code; Part 2 alone has nothing to call).
- Part 3 can land independently after 1–2 — fix path stays correct.
- Parts 5/6/7 can land in any order after 1–3 (+ ideally 4); they're independent capability adds.
- If Part 4 (cache) is delayed, gate 5–7 behind env flag `OPENBOT_GROUNDED_RESPONDERS=false` so prod cost doesn't regress.

---

## Acceptance for the whole slice

- [ ] All 7 parts merged.
- [ ] `make check` green at every commit.
- [ ] E2E demo for each workflow (triage/review/fix/chat) passes with sandbox-grounded responder.
- [ ] No double-clone in any workflow (dispatcher integration test).
- [ ] Classifier-bypass eval suite gates CI.
- [ ] Spec + plan archived to `docs/_archive/superpowers/`.

---

## Retro — Parts 1–3 (2026-05-21)

Captured after `3a0ab50` (Part 3) landed, before Parts 4–7 start. 14 commits, 1078 → 1097 tests, hexagonal contract held throughout, no `--no-verify` invocations.

### What worked

- **Type & symbol contract table** (top of plan): every later part referenced the table by symbol name. Zero "what was that field called?" thrash mid-implementation. Spec ↔ plan name drift caught at Part 1 review, fixed once.
- **TDD discipline on pure functions** (`derive_sandbox_policy`, `resolve_checkout`): 11-cell parametrized matrix on `resolve_checkout` caught two ref-resolution corner cases (PR review comment fallback; inline-comment commit_id vs head_sha) before any dispatcher wiring. Cost: ~15 min upfront, saved ~45 min of debugging in Part 2.
- **Late-bottom-of-file imports**: `derive_sandbox_policy` lives in `application/sandbox_policy.py` but imports `Feature` from `domain/workflows.py`; `Feature` is a domain-side enum and the application module is shallower. The contract check (`lint-imports`) catches reverse-direction imports — pushing imports to bottom of file (after definitions) kept the module tree acyclic without TYPE_CHECKING gymnastics.
- **`_LabelledSentryCounter` generic wrapper**: consolidated Prometheus + Sentry mirror call sites into one labelled-counter abstraction. Adding `dispatch_sandbox_total` and `classifier_error_total` in Task 2.3 took ~10 LoC each instead of ~30.

### What we learned the hard way

- **Cause-ordered precedence > symptom-ordered checks.** Task 2.3's RED test expected `bypass_source="classifier"` but production code returned `"degrade"` because the check order was: factory missing? → degrade. classifier said skip? → classifier. The classifier reason was the *cause*; the missing factory was a *symptom* of the same decision. Fix: order checks by cause (static → classifier → degrade), not by which `if` branch happens to match first. **Heuristic for future:** when a labelled counter has multiple precedences, write the labels in cause order on paper before coding the `if`/`elif`.
- **E2E fake-adapter port-coverage gap.** Part 1 added `get_default_branch_sha` and `get_pull_request` to `ChannelAdapterPort`. Unit tests passed because `tests/_fakes/channel_adapter.py` got both methods. E2E tests (`tests/e2e/conftest.py`'s `FakeChannelAdapter`) failed because the E2E fake is *a separate file* that wasn't touched. Both adapters claim to implement the same Protocol but Python's structural typing only catches the mismatch at call-time. **Heuristic for future:** when adding a Protocol method, `grep -rn "class.*ChannelAdapter\(.*Port" tests/` (any file with "channel_adapter" in path) and update every fake in one commit.
- **`sys.modules` monkeypatch shim idiom is now load-bearing.** The dispatcher imports `classify_for_dispatch`, `resolve_checkout`, `load_for_repo`, `run_preflight`, and two Prometheus counters — all overridable in tests via `monkeypatch.setattr(sys.modules["openbot.application.dispatcher"], "name", fake)`. Production code accesses them through *its own module's namespace* (e.g. `from openbot.application.dispatcher import resolve_checkout as _resolve`). This keeps tests fast (no DI plumbing) but means **the late-binding contract is implicit**. Worth a comment at the top of dispatcher.py noting which symbols are intended as monkeypatch seams.
- **Removing `_CLONE_FAIL` user-facing template.** Once the dispatcher owns clone failures, the use-case-level "could not clone" reply became dead code. The `bypass_source="degrade"` counter is the new SRE-side signal; users see the generic `_NO_SANDBOX` text because the cause distinction doesn't help them but does help dashboards. **Heuristic for future:** when moving an error from one layer to another, ask "who needed to *act* on this message?" — if it's ops, route to metrics; if it's the user, route to copy. Don't keep both.

### Mechanics that paid off

- **Per-task commits, not per-part.** 9 commits for Part 1 (one per task) made bisect cheap when Task 1.6 (clone strategies) silently broke a Daytona snapshot test in Task 1.9 — `git bisect run` pointed at the exact commit in 4 steps.
- **`make check` green at every commit, never `--no-verify`.** Pre-commit hooks caught two ruff fmt-check regressions (auto-fixed) and one import-sort issue (fixed with `ruff check --fix`). Total recovery time: ~30s each.
- **Status checkpoint table at the top of this plan.** Added after Part 3 landed, before Parts 4–7. Future sessions can resume from "Parts 1–3 ✅, Part 4 split, 5–7 pending" without reading 540 lines.

### Open questions for Part 4+

- **Snapshot cache key shape.** `(repo_url, ref, strategy)` is the obvious key, but `strategy=BLOBLESS` vs `SHALLOW` produces different working-tree shapes for the same `ref`. Spec needs to nail this before any caching code.
- **Classifier-bypass eval suite (cross-cutting concern).** Listed at line 519 but not built yet. Should land alongside Part 5 (triage) since triage is where the bypass-precision risk is highest (classifier false-positive `NO_SANDBOX` on a real bug).
- **Budget-exhausted counter.** Cross-cutting concerns list mentions `openbot_responder_budget_exhausted_total{feature}` but it's not implemented. Add when Part 5's responder lands (first new responder with a real budget).

### Don't repeat

- Don't substitute *any* checkbox-marking commit for a status-checkpoint table. Task-level checkboxes are sprint state; the checkpoint table is the human-readable summary. Both can coexist; the table is what readers actually use.
- Don't move this plan to `docs/_archive/superpowers/` until Parts 5–7 land. The plan is still active reference for those parts.
