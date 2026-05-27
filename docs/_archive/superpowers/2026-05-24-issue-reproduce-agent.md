# Issue-Reproduce Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the placeholder triage ACK into an end-to-end reproduce pipeline: for `type=bug` issues with reproduction info, run a bounded agent inside the pre-provisioned sandbox, produce a structured `ReproOutcome`, and edit the same sticky comment to show the result.

**Architecture:** Three foundation modules land first (independent + parallelisable), then the profile + tools layer, then the use case wiring, finally E2E. One PR per slice (R1–R5). Each PR ≤ ~400 lines diff. The reproduce-trigger predicate (use case) and the sandbox policy (dispatcher) must always agree — pin that with a paired test.

**Tech Stack:** Python 3.12, FastAPI, LangChain agent middleware, DeepAgents, LangGraph, pytest (`asyncio_mode=auto`), `uv` for deps.

---

## Spec source

`docs/superpowers/specs/2026-05-24-issue-reproduce-agent-design.md` (reviewed pass applied 2026-05-25 — A7 added, `__post_init__` → tests, `_safe_update` → extended `sticky_reply`, `write_repro_script` deferred, trace span renamed, contract test fixed).

---

## Branch strategy

Branch from `origin/main`, linear stack — each slice merges before the next begins.

```bash
git fetch origin
git checkout -b feat/repro-foundation origin/main
```

| Order | Branch | Base | Scope |
|---|---|---|---|
| R1 | `feat/repro-foundation` | `origin/main` | `domain/repro.py` + `_repro_schema.py` + `sticky_reply` extension + `sandbox_policy` A7 |
| R2 | `feat/repro-profile` | `origin/main` after R1 | `_repro_tools.py` + `deepagents_repro.py` |
| R3 | `feat/repro-render` | `origin/main` after R2 | `_repro_render.py` (pure formatting) |
| R4 | `feat/repro-use-case` | `origin/main` after R3 | `triage.py` reproduce branch + integration tests |
| R5 | `feat/repro-e2e` | `origin/main` after R4 | Docker E2E + CHANGELOG |

R1 is the only slice that touches a "locked boundary" (`sandbox_policy.py` per spec A7) — review carefully.

---

## File structure

### New files

| Path | Responsibility |
|---|---|
| `openbot/domain/repro.py` | `ReproStatus` enum + frozen `ReproOutcome` dataclass (invariants asserted in tests, not `__post_init__`). |
| `openbot/infrastructure/agents/_repro_schema.py` | Pydantic `ReproOutcomeSchema` + `parse_structured_response` → `ReproOutcome`. |
| `openbot/infrastructure/agents/_repro_tools.py` | `make_repro_tools(sandbox, event)` → `[read_file, list_files, run_command]`. Re-uses the existing `_fix_tools` read-only wrappers. |
| `openbot/infrastructure/agents/deepagents_repro.py` | `ReproProfile` + `DeepAgentsReproResponder` (delegates to `BaseDeepAgentRuntime`). |
| `openbot/application/use_cases/_repro_render.py` | Pure render functions: `render_thinking_comment(actor)`, `render_final_comment(outcome, *, ack_only=False)`. |
| `tests/domain/test_repro.py` | Value-type tests + invariant assertions. |
| `tests/infrastructure/agents/test_repro_schema.py` | Schema parse / missing-field / coercion tests. |
| `tests/infrastructure/agents/test_repro_tools.py` | Tool factory: returned names, sandbox is required, run_command timeout clamp. |
| `tests/infrastructure/agents/test_deepagents_repro.py` | Profile contract, prompts, parse_result, limits, **forbidden-set contract**. |
| `tests/application/use_cases/test_repro_render.py` | Snapshot per status + XSS defence + truncation. |
| `tests/application/use_cases/test_triage_reproduce.py` | Use case integration matrix (~8 rows). |
| `tests/application/test_sandbox_policy_repro.py` | A7 policy table + paired-with-predicate test. |
| `tests/e2e/test_triage_repro_e2e.py` | Docker + fake LLM happy path. |

### Modified files

| Path | Change |
|---|---|
| `openbot/application/use_cases/_lifecycle.py` | Extend `_StickyReply` with `fallback_on_update_error: bool = False`. Default keeps fix.py behaviour. |
| `openbot/application/sandbox_policy.py` | A7: triage REQUIRED iff `type == "bug" AND has_reproduction_info`; everything else NO_SANDBOX. |
| `openbot/application/use_cases/triage.py` | Add reproduce branch + `_should_run_reproduce`, `_generate_repro_outcome`. Switch existing ACK path to `sticky_reply(..., fallback_on_update_error=True)`. |
| `openbot/infrastructure/agents/__init__.py` | Re-export `DeepAgentsReproResponder`. |
| `openbot/domain/__init__.py` | Re-export `ReproOutcome` + `ReproStatus`. |
| `CHANGELOG.md` | Append to `[Unreleased]` block once R5 ships. |

---

## Workstream R1 — Foundation

Goal: ship the three independent value-layer changes (`domain/repro.py`, schema, `sticky_reply` extension, policy A7) so R2–R4 have stable types to depend on.

### Task R1.1 — `domain/repro.py` + tests

- [ ] Write `tests/domain/test_repro.py` first (red):
  - `ReproStatus` enum values are `{"reproduced","not_reproduced","insufficient_info","agent_failed"}` (lock serialisation).
  - `ReproOutcome` is frozen (mutation raises `FrozenInstanceError`).
  - Construction with all fields succeeds; missing required field raises `TypeError`.
  - **Invariants asserted in tests**, mirroring `domain/fix.py`'s docstring:
    - `repro_artifact` and `repro_artifact_filename` are both `None` or both set.
    - `status == REPRODUCED` ⇒ `command is not None and repro_artifact is not None`.
    - `status == INSUFFICIENT_INFO` ⇒ `command is None and repro_artifact is None`.
    - `len(output_excerpt) <= 2000` (responder enforces; test pins the contract).
- [ ] Implement `openbot/domain/repro.py` (green) — frozen dataclass, no `__post_init__`. Copy the rationale comment from `domain/fix.py` so future readers know why invariants are test-only.
- [ ] Re-export in `openbot/domain/__init__.py` if a package-level export pattern exists; otherwise leave imports explicit.
- [ ] Verify: `uv run pytest tests/domain/test_repro.py -q`.

### Task R1.2 — `_repro_schema.py` + tests

- [ ] Write `tests/infrastructure/agents/test_repro_schema.py` first (red):
  - Valid `ReproOutcomeSchema` dict → `parse_structured_response` returns matching `ReproOutcome`.
  - Missing required field → `AgentStructuredOutputError`.
  - `status` not in the enum → `AgentStructuredOutputError`.
  - `output_excerpt` longer than 2000 chars → truncated with `…[truncated]` suffix, total ≤ 2000.
- [ ] Implement `openbot/infrastructure/agents/_repro_schema.py` modelled on `_fix_schema.py`:
  - Pydantic v2 `BaseModel` with `status: Literal[...]`, `summary`, `command`, `exit_code`, `output_excerpt`, `hypothesis`, `repro_artifact`, `repro_artifact_filename`.
  - `parse_structured_response(data: Mapping[str, Any]) -> ReproOutcome` — Pydantic stops here, the function returns the domain dataclass.
- [ ] Verify: `uv run pytest tests/infrastructure/agents/test_repro_schema.py -q`.

### Task R1.3 — Extend `sticky_reply` with `fallback_on_update_error`

- [ ] Write `tests/application/use_cases/test_lifecycle_sticky_fallback.py` (red):
  - Default behaviour (no flag): `update()` swallows error, no extra `reply` call. **Re-asserts** existing fix.py semantics.
  - `fallback_on_update_error=True`: when `update_comment` raises, exactly one extra `reply()` call is made with the same body.
  - Both fallbacks raising → no exception propagates; both errors logged at `exception` level.
  - `comment_id is None` (initial POST failed) → both branches are no-ops regardless of flag.
- [ ] Implement: add `fallback_on_update_error: bool = False` to `_StickyReply`; thread it through the `sticky_reply` context manager kwarg. Add the fallback branch inside `update()`.
- [ ] Audit `fix.py` callers — none pass the flag; behaviour unchanged.
- [ ] Verify: `uv run pytest tests/application/use_cases/ -q`.

### Task R1.4 — `sandbox_policy.py` A7 tightening + paired test

- [ ] Write `tests/application/test_sandbox_policy_repro.py` (red) — table-driven:

  | `type` | `has_reproduction_info` | `looks_like_spam` | Expected |
  |---|---|---|---|
  | bug | True | False | REQUIRED |
  | bug | False | False | NO_SANDBOX |
  | feature_request | False | False | NO_SANDBOX |
  | feature_request | True | False | NO_SANDBOX |
  | question | True | False | NO_SANDBOX |
  | other | True | False | NO_SANDBOX |
  | bug | True | True | NO_SANDBOX |

  Plus the **paired invariant** test:

  ```python
  @pytest.mark.parametrize("type_,has_repro", [...])
  def test_policy_and_predicate_agree(type_, has_repro):
      # _should_run_reproduce(ctx) === (policy(ctx) is REQUIRED)
      ...
  ```

- [ ] Update `_TRIAGE_BYPASS_TYPES_WITHOUT_REPRO` block in `openbot/application/sandbox_policy.py` per spec §4.2:

  ```python
  if feature is Feature.TRIAGE and isinstance(output, TriageClassifierOutput):
      if output.looks_like_spam:
          return SandboxPolicy.NO_SANDBOX
      if not (output.type == "bug" and output.has_reproduction_info):
          return SandboxPolicy.NO_SANDBOX
  ```

  Remove the now-unused `_TRIAGE_BYPASS_TYPES_WITHOUT_REPRO` constant if no other module references it (grep first).
- [ ] Run the *existing* `tests/application/test_sandbox_policy.py` — if any case breaks, it's catching a real regression in upstream callers and needs a paired fix in this PR. Do not skip or weaken existing assertions.
- [ ] Verify: `make check`.

### Task R1.5 — Commit + open R1 PR

- [ ] `git add -A && git commit -m "feat(repro): foundation — domain, schema, sticky fallback, policy A7"`.
- [ ] `git push -u origin feat/repro-foundation` and open PR via `gh pr create`.
- [ ] PR body links the spec and lists R2–R5 as follow-ups.

---

## Workstream R2 — Profile + tools

Goal: ship `ReproProfile` so the use case has something to call.

### Task R2.1 — `_repro_tools.make_repro_tools`

- [ ] Write `tests/infrastructure/agents/test_repro_tools.py` (red):
  - `make_repro_tools(sandbox=Stub(), event=...)` returns tools with `.name in {"read_file", "list_files", "run_command"}` — exactly three, no extras.
  - `read_file` delegates to `sandbox.read_file(path)`.
  - `list_files` delegates to `sandbox.list_files(path, max)`.
  - `run_command` clamps `timeout_seconds` to ≤ 300 even if the LLM passes 9999 (mirrors `_fix_tools`).
  - No reference to `write_file`, `git_diff`, or `search_files` in the returned set.
- [ ] Implement `openbot/infrastructure/agents/_repro_tools.py`:
  - Import the three callables from `_fix_tools.make_fix_tools` if they're already factored as standalone helpers — otherwise copy the three closures verbatim. **Do not** export `write_file` / `git_diff` / `search_files`.
  - Keep the factory signature `make_repro_tools(*, sandbox, event)` consistent with `make_fix_tools`.
- [ ] Verify: `uv run pytest tests/infrastructure/agents/test_repro_tools.py -q`.

### Task R2.2 — `ReproProfile` + `DeepAgentsReproResponder`

- [ ] Write `tests/infrastructure/agents/test_deepagents_repro.py` (red):
  - `ReproProfile.feature is Feature.TRIAGE`.
  - `ReproProfile.agent_name == "repro"` (no `_profile` suffix — matches `review`/`fix`/`chat`).
  - `ReproProfile.sandbox_requirement is SandboxRequirement.REQUIRED`.
  - `ReproProfile.checkpoint_enabled is True`.
  - `ReproProfile.limits` matches spec §3.4 (recursion_limit=40, model_call_limit=15, tool_call_limit=30, wall_seconds=180, max_output_tokens=4000, thinking_budget_tokens=0).
  - `system_prompt(request)` contains the `INSUFFICIENT_INFO` early-exit instruction.
  - `user_message(request)` contains `issue_title`, `issue_body`, and `base_sha`.
  - **Forbidden-set contract test:**
    ```python
    tools = profile.build_tools(request)
    names = {t.name for t in tools}
    assert names <= {"read_file", "list_files", "run_command"}
    assert names.isdisjoint({"write_file", "git_diff", "search_files"})
    ```
  - `parse_result(valid_dict)` returns a `ReproOutcome`.
  - `parse_result(missing_field_dict)` raises `AgentStructuredOutputError`.
  - `build_tools(request_without_sandbox)` raises `AgentSandboxRequiredError`.
- [ ] Implement `openbot/infrastructure/agents/deepagents_repro.py`:
  - Mirror `deepagents_fix.py` structure: `@dataclass FixProfile` → `@dataclass ReproProfile`, identical `BaseDeepAgentRuntime` delegation in `DeepAgentsReproResponder`.
  - System prompt: bounded, explicit about early-exit, no fix-loop language.
- [ ] Re-export `DeepAgentsReproResponder` from `openbot/infrastructure/agents/__init__.py`.
- [ ] Verify: `make check`.

### Task R2.3 — Commit + R2 PR

- [ ] `git commit -m "feat(repro): profile + read-only tools"`.
- [ ] Open PR; assignee reviews the forbidden-set contract test.

---

## Workstream R3 — Render

Goal: pure-function comment rendering, no I/O, easy to snapshot-test.

### Task R3.1 — `_repro_render` + tests

- [ ] Write `tests/application/use_cases/test_repro_render.py` (red):
  - `render_thinking_comment(actor="alice")` returns non-empty string containing `":robot:"` and `"@alice"`.
  - `render_final_comment(None, ack_only=True)` ≡ existing ACK template (snapshot against current `triage.py:_ACK_TEMPLATE`).
  - Status snapshots — one each:
    - `REPRODUCED` → contains `":white_check_mark:"` and a `<details>` block with the fenced `repro_artifact`.
    - `NOT_REPRODUCED` → contains `":warning:"` and the command + exit_code.
    - `INSUFFICIENT_INFO` → contains `":speech_balloon:"` and the hypothesis as a missing-info list.
    - `AGENT_FAILED` → contains `":robot:"` and the §5.3 user-visible string. **No exception class name** in the output.
  - `output_excerpt` longer than 2000 chars: rendered with `…[truncated]` marker, total render ≤ 4000 chars overall (snapshot length budget).
  - **XSS defence**: outcome with `output_excerpt="</details><script>alert(1)</script>"` does not produce raw `<script>` in the rendered output. Either fenced-codeblock-broken or HTML-escaped is acceptable; assert both `<script` and unescaped `</details>` are absent inside the fenced block.
- [ ] Implement `openbot/application/use_cases/_repro_render.py`:
  - Pure functions, no logging, no `await`.
  - Use Python f-strings + `textwrap.dedent`; no Jinja, no Markdown lib.
  - Inline HTML escape helper (or `html.escape`) on `output_excerpt`, `hypothesis`, `summary` before any `<details>` interpolation.
- [ ] Verify: `make check`.

### Task R3.2 — Commit + R3 PR

- [ ] `git commit -m "feat(repro): comment renderers"`.

---

## Workstream R4 — Use case wiring

Goal: connect everything in `triage.py` and pin behaviour with the integration matrix.

### Task R4.1 — Use case integration tests first (red)

- [ ] Write `tests/application/use_cases/test_triage_reproduce.py` against the matrix from spec §6.3, **all rows red**:

  | Test | classifier | sandbox | Expected audit | Comments |
  |---|---|---|---|---|
  | `test_no_classifier_falls_back_to_ack` | None | None | `ack_only` | thinking → ack_only |
  | `test_non_bug_skips` | type=question | None | `ack_only` | thinking → ack_only |
  | `test_bug_without_repro_info_skips` | bug, repro=False | None | `ack_only` | thinking → ack_only |
  | `test_bug_with_repro_info_runs_agent` | bug, repro=True | provisioned | `reproduce:reproduced` | thinking → reproduced |
  | `test_agent_exception_renders_failed_template` | bug+repro | provisioned + responder raises `AgentTimeoutError` | `reproduce:agent_failed:AgentTimeoutError` | thinking → agent_failed |
  | `test_placeholder_post_failure_runs_agent_anyway` | bug+repro | provisioned + `adapter.reply` raises | `reproduce:reproduced:no_comment` | none (sticky no-ops; outcome in audit) |
  | `test_update_comment_fails_falls_back_to_reply` | bug+repro | provisioned + `update_comment` raises | `reproduce:reproduced` | thinking + final (two `reply` calls via fallback) |
  | `test_cancellation_propagates_and_writes_cancelled` | bug+repro | provisioned + checkpoint raises `RunCancelledError` | `CANCELLED` row | thinking only, no final update |
  | `test_predicate_matches_policy` | parametrised | parametrised | — | invariant: predicate ≡ policy (R1.4 paired check, also lives here) |

- [ ] Use `preflight_ctx_factory` (existing fixture) and monkeypatch `_generate_repro_outcome`.

### Task R4.2 — Implement reproduce branch (green)

- [ ] Modify `openbot/application/use_cases/triage.py`:
  - Add `_should_run_reproduce(ctx) -> bool` (per spec §4.2).
  - Add `_generate_repro_outcome(ctx) -> ReproOutcome` async helper — wraps `DeepAgentsReproResponder.run(AgentRequest(...))`, translates `AgentError` subclasses to `AGENT_FAILED` outcome with `command=None, exit_code=None`, leaves `RunCancelledError` to propagate.
  - Inside `async with audit_lifecycle(...)`:
    ```python
    async with sticky_reply(adapter, event,
                            initial=render_thinking_comment(actor=event.actor),
                            fallback_on_update_error=True) as sticky:
        if not _should_run_reproduce(ctx):
            await sticky.update(render_final_comment(None, ack_only=True))
            audit.outcome = "ack_only"
            return
        try:
            outcome = await _generate_repro_outcome(ctx)
        except (AgentError, Exception) as exc:
            outcome = ReproOutcome(status=AGENT_FAILED, ...)
            audit.outcome = f"reproduce:agent_failed:{type(exc).__name__}"
        else:
            suffix = ":no_comment" if sticky.comment_id is None else ""
            audit.outcome = f"reproduce:{outcome.status.value}{suffix}"
        await sticky.update(render_final_comment(outcome))
    ```
  - `finally` outside the `async with`: `try: await checkpointer.adelete_thread(run_id) except Exception: _logger.exception(...)` — only when `checkpointer is not None`.
- [ ] Verify: `make check`. All R4.1 rows should now pass.

### Task R4.3 — Commit + R4 PR

- [ ] `git commit -m "feat(repro): wire reproduce branch into triage use case"`.
- [ ] PR description calls out the policy/predicate paired invariant and links the R1 PR.

---

## Workstream R5 — E2E + ship

### Task R5.1 — Docker E2E happy path

- [ ] Write `tests/e2e/test_triage_repro_e2e.py`:
  - Reuse `tests/e2e/conftest.py` Docker sandbox, `LocalChannelAdapter`, `fake_llm` fixture.
  - POST `issue.opened` webhook with bug + repro-info body → wait for worker.
  - Assert `LocalChannelAdapter` received exactly one `reply` (placeholder) + exactly one `update_comment` (final).
  - Assert final body contains `:white_check_mark:` or `:warning:`.
  - Assert exactly one `audit_log` row with `outcome LIKE 'reproduce:%'` for this delivery_id.
- [ ] Verify: `uv run pytest tests/e2e/test_triage_repro_e2e.py -q` (Docker required).

### Task R5.2 — CHANGELOG + flip status

- [ ] Append to `CHANGELOG.md` `[Unreleased]`:
  ```markdown
  ### Added
  - Triage reproduce stage: bug issues with reproduction info now run a bounded
    sandbox agent and post the outcome to the same sticky comment. Tool surface
    is read-only (read_file, list_files, run_command); no working-tree mutation.
  ### Changed
  - `derive_sandbox_policy` now requires `type == "bug" AND has_reproduction_info`
    for triage. Non-reproducible triage no longer provisions a sandbox.
  - `_StickyReply.update` accepts `fallback_on_update_error` to post a second
    comment via `reply()` if the `update_comment` PATCH fails.
  ```
- [ ] `git commit -m "feat(repro): e2e + changelog"`.
- [ ] Open final PR — body summarises R1–R5 link chain and points at the spec.

### Task R5.3 — Archive spec + plan

- [ ] After R5 merges:
  ```bash
  mv docs/superpowers/specs/2026-05-24-issue-reproduce-agent-design.md \
     docs/_archive/superpowers/
  mv docs/superpowers/plans/2026-05-24-issue-reproduce-agent.md \
     docs/_archive/superpowers/
  ```
- [ ] Commit on `main`: `chore: archive reproduce-agent spec + plan`.

---

## Verification matrix

| Layer | Command | Expected |
|---|---|---|
| Unit (domain) | `uv run pytest tests/domain/test_repro.py -q` | 5 tests pass |
| Unit (schema) | `uv run pytest tests/infrastructure/agents/test_repro_schema.py -q` | 4 tests pass |
| Unit (tools) | `uv run pytest tests/infrastructure/agents/test_repro_tools.py -q` | 5 tests pass |
| Unit (profile) | `uv run pytest tests/infrastructure/agents/test_deepagents_repro.py -q` | 9 tests pass (incl. forbidden-set) |
| Unit (render) | `uv run pytest tests/application/use_cases/test_repro_render.py -q` | 8 tests pass (incl. XSS) |
| Unit (sticky) | `uv run pytest tests/application/use_cases/test_lifecycle_sticky_fallback.py -q` | 4 tests pass |
| Policy | `uv run pytest tests/application/test_sandbox_policy_repro.py -q` | 7-row matrix + paired invariant |
| Use case | `uv run pytest tests/application/use_cases/test_triage_reproduce.py -q` | 8 rows + invariant pass |
| Full suite | `make check` | green at every R*.1 / R*.2 boundary |
| E2E | `uv run pytest tests/e2e/test_triage_repro_e2e.py -q` | 1 test pass (Docker) |

---

## Coverage targets

| File | Target |
|---|---|
| `openbot/domain/repro.py` | 100% |
| `openbot/application/use_cases/_repro_render.py` | 100% |
| `openbot/infrastructure/agents/_repro_schema.py` | ≥ 95% |
| `openbot/infrastructure/agents/_repro_tools.py` | ≥ 95% |
| `openbot/infrastructure/agents/deepagents_repro.py` | ≥ 85% |
| `openbot/application/use_cases/triage.py` (changed lines) | ≥ 90% |
| `openbot/application/sandbox_policy.py` (changed lines) | 100% |

---

## Out of scope (do not implement here)

- Eval suite (`evals/tasks/reproduce_*.py`, `evals/solvers/repro.py`, `evals/scorers/repro_score.py`) — separate slice once we have ≥10 labelled issues. Spec §1.2.
- `write_repro_script` and on-disk script artifact — deferred to v0.2 fix-handoff. Spec §3.5.
- Runtime `$0.20/issue` cost meter — proxy ceilings (wall_seconds + model_call_limit) carry v0.1; Langfuse handles post-hoc audit. Spec §1.2.
- Issue dedup, auto-close, label/priority pipeline upgrades — PRD §4.1 punts these.

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Policy change in R1 breaks an unrelated triage test | Medium | R1.4 runs existing `tests/application/test_sandbox_policy.py` before adding new rows; any breakage is fixed in the same PR, not deferred. |
| `sticky_reply` fallback double-posts in CI flake | Low | R3 snapshots assert single-comment happy path; integration test row pins the fallback. |
| Agent tool wrappers diverge from `_fix_tools` over time | Medium | R2.1 prefers importing the existing helpers; if copy-pasted, add a comment pointing back to `_fix_tools.make_fix_tools` so the next maintainer keeps them in sync. |
| LangSmith span name `repro` collides with another span | Low | Grep first: `grep -rn 'agent_name.*"repro"' openbot/` before adding the literal. |
| `domain.fix`-style "invariants in tests" convention forgotten in PR review | Low | R1.1 task body explicitly cites `domain/fix.py`'s docstring; reviewers see the rationale inline. |
