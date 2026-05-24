# v0.1 Product Closure — Current gaps from PRD to runnable GitHub-only alpha

**Status:** design. Awaiting implementation plan.
**Date:** 2026-05-22
**Branch (proposed):** `feat/v0-1-product-closure`
**PRD anchors:** §1 (four core workflows), §4.1 (triage), §4.2 (review), §4.3 (fix), §4.4 (chat), §4.5 (cost caps), §4.6 (rate limits), §4.7 (cancellation), §4.8 (security), §5.1 (webhook to worker flow), §8 (quality and evals).
**Related specs:** `2026-05-21-unified-sandbox-entry-design.md`, `2026-05-21-queue-simplification-design.md`, `2026-05-21-sandbox-snapshot-cache-design.md`, `2026-05-22-deepagents-runtime-design.md`.

---

## Goal

Close the gap between the PRD and the current codebase so OpenBot reaches a
dogfoodable **v0.1 GitHub-only alpha**:

1. A signed GitHub webhook is accepted once, routed, preflighted, enqueued,
   consumed by the worker, and executed by the correct workflow handler.
2. The four v0.1 workflows have honest user-visible behavior:
   triage, review, fix, and chat.
3. Safety and cost controls are enforced at the actual execution points, not
   just represented as config or schema.
4. Tests describe the current entrypoints and no longer assert deleted
   transitional APIs.

This spec is a current-state closure document. It is not a rewrite of the PRD.
The PRD remains the product target; this document defines what must be made true
in the repository before calling v0.1 alpha runnable.

---

## Product vision summary

OpenBot is an open-source, self-hosted GitHub maintainer bot for individual OSS
maintainers who want to keep their own GitHub App, LLM API keys, prompts, data,
and cost controls.

v0.1 is GitHub-only. The MVP surface is:

| Workflow | Trigger | Required v0.1 user outcome |
|---|---|---|
| Triage | `issues.opened` | Labels, priority, and when useful a reproduce/evidence comment. |
| Review | `pull_request.opened` / `pull_request.synchronize` | Severity-filtered PR review comments, never merge/block by default. |
| Fix | issue assigned to the bot | Sandbox-backed code change, tests, branch push, PR creation, never auto-merge. |
| Chat | `@openbot ...` comment | Read-only repo-grounded answer or explicit refusal/clarification. |

The differentiator is not "yet another code agent." It is the combination of
OSS, self-hosting, BYO API key, explicit GitHub App ownership, multi-channel
architecture seams, plugin trajectory, and public evals.

---

## Current implementation snapshot

### Completed or mostly complete

| Area | Current state | Evidence |
|---|---|---|
| GitHub channel boundary | `GitHubAdapter` exists and the FastAPI route delegates to `ingest_webhook`. | `openbot/entrypoints/api/routes/github_webhook.py`, `openbot/application/use_cases/ingest_webhook.py` |
| Routing | Router maps issue, PR, assignment, label, and mention events to the four features. | `openbot/application/router.py` |
| Config loading | `.openbot/config.yaml` loader exists with baked-in defaults for feature toggles, budgets, rate limits, cancel, fork PR, and severity threshold. | `openbot/infrastructure/config_loader.py` |
| Preflight chain | Sanitize, kill switch, feature toggle, cancel label/comment, fork PR gate, actor role, rate limit, budget, and audit-start middlewares are assembled in one order. | `openbot/application/dispatcher.py` |
| Persistence schema | `cost_meter`, `audit_log`, and `task_runs` SQLAlchemy models exist. | `openbot/infrastructure/persistence/models.py` |
| Review workflow | Structured DeepAgents review responder, severity filtering, PR Review API submission, and fallback review are implemented. | `openbot/application/use_cases/review.py`, `openbot/infrastructure/agents/deepagents_review.py` |
| Fix workflow body | Handler logic exists for issue fetch, sandbox-backed DeepAgents fix, branch creation, push, PR open, test-failed reply, and failure templates. | `openbot/application/use_cases/fix.py`, `openbot/infrastructure/agents/deepagents_fix.py` |
| Sandbox port | Production `SandboxPort` and Daytona adapter exist. | `openbot/application/ports/sandbox.py`, `openbot/infrastructure/sandboxes/daytona.py` |
| Eval suite | Review, fix, test-generation, and chat offline eval surfaces are implemented under Inspect AI. | `evals/README.md`, `evals/tasks/*`, `evals/solvers/*` |

### Partially complete or misleading

| Area | Current state | Closure needed |
|---|---|---|
| Webhook to worker contract | API ingress enqueues `QueuePayload`; worker currently deserializes only `TaskSpec v3`. | One queue contract must be the production contract. |
| Worker sandbox wiring | `execute_handler` accepts `sandbox_factory`; worker entrypoint does not pass one into `consume_loop` / handler execution. | Worker must create and inject the configured sandbox backend. |
| Triage | Handler posts an ACK only and explicitly says auto-label, priority, and reproduce are future work. | Implement the real triage path or cut scope honestly. |
| Chat | Freeform chat uses a DeepAgent with no tools. Help/cancel parsing exists. | Add read-only repo-grounded tools and refusal rules. |
| Budget | Monthly/global budget gates exist in preflight; per-task agent-loop enforcement is not yet at each LLM/tool step. | Add runtime-level per-task budget checks. |
| Cancellation | Input-side label/comment/env gates exist; long-running agent-loop checkpoints are only partially represented. | Ensure every long-running loop checks cancellation at bounded intervals. |
| Secret scanning | Developer hooks and CI TruffleHog exist; bot-authored GitHub output is not clearly scanned before egress. | Add output egress scanner around replies/reviews/PR bodies. |
| Model routing | PRD says Claude Opus/Sonnet defaults; code currently routes all features to `anthropic/GLM-5.1` via proxy. | Decide whether this is the new target and update PRD/config example, or restore PRD routing. |
| Sandbox provider | PRD says Modal, implementation/deploy docs point to Daytona. | Decide and align docs, config, env names, and product copy. |
| Tests | Some tests still import deleted `run_dispatch`; queue tests expose worker/contract drift. | Migrate tests to current entrypoints before claiming stability. |

---

## Hard blockers before dogfood

### 1. Queue contract mismatch

The largest current blocker is the production path:

```
GitHub webhook
  -> ingest_webhook(...)
  -> queue.enqueue(...)
  -> Redis stream entry containing QueuePayload
  -> worker._process_entry(...)
  -> deserialize_task_spec(...)
  -> DLQ / skip because this is not TaskSpec v3
```

The worker docstring says it consumes `TaskSpec v3`. The API path still writes
through `QueuePort.enqueue(...)`, whose Redis implementation builds a
`QueuePayload`.

Locked decision:

| Topic | Decision |
|---|---|
| Production queue schema | `TaskSpec v3` is the only production worker input for v0.1 closure. |
| Legacy `QueuePayload` | Keep deserialization only if needed for rolling-upgrade compatibility, but the API receive path must not produce it. |
| Preflight location | Preflight runs before building `TaskSpec v3`; worker trusts the spec and does not rerun preflight. |
| Worker behavior on legacy payload | Either support a narrow v1/v2 upgrade path intentionally or DLQ with a distinct `legacy_queue_payload_unreadable` reason. Do not silently treat every non-v3 blob as malformed JSON. |

Acceptance:

1. A webhook accepted by `/webhook/github` writes a `TaskSpec v3` stream entry.
2. `consume_loop` consumes that entry and calls `execute_handler` once.
3. A test proves PR check-run creation survives the new queue path.
4. No test imports `run_dispatch`.

### 2. Worker sandbox injection missing

`execute_handler` already has the correct seam:

```
execute_handler(..., sandbox_factory=None, sandbox_cache=None, ...)
```

The worker entrypoint does not construct a `DaytonaSandboxAdapter.create`
factory or pass it to `consume_loop`, and `consume_loop` does not accept a
sandbox factory today. As a result, production fix tasks degrade to the
`_NO_SANDBOX` reply even when `OPENBOT_DAYTONA_API_KEY` is configured.

Locked decision:

| Topic | Decision |
|---|---|
| v0.1 production sandbox | Use Daytona unless the PRD is explicitly changed back to Modal. The code, Heroku docs, app.json, and dependency set already point to Daytona. |
| Factory creation | Worker composition root owns sandbox backend selection from settings. |
| Handler contract | Handlers still receive `ctx.sandbox_handle` or `None`; handlers never create sandboxes. |
| Missing sandbox config | Fix posts a clear degrade comment and the dispatch metric marks `bypass_source="degrade"`. |

Acceptance:

1. With `OPENBOT_DAYTONA_API_KEY` unset, fix returns the current no-sandbox
   degrade reply.
2. With a fake sandbox factory injected in tests, fix reaches
   `_generate_fix_outcome`.
3. Worker entrypoint logs which sandbox backend is configured.
4. No production path relies on handler-internal cloning.

### 3. Tests no longer describe the code

The current light test run found:

1. `tests/application/test_dispatcher.py` imports deleted `run_dispatch`.
2. API check-run tests still assert old dispatch behavior.
3. Queue tests expose v3 worker assumptions and stale DLQ reason expectations.
4. Some async mocks create unawaited coroutine warnings because tests no longer
   match the current adapter/config-loader call shape.

Locked decision:

| Topic | Decision |
|---|---|
| Test source of truth | Tests should target `ingest_webhook`, `decide_and_enqueue` where still used, `TaskSpec`, and `execute_handler`. |
| Deleted API | Do not restore `run_dispatch` just to satisfy stale tests. |
| Queue tests | Split producer contract tests from worker consumer tests. |

Acceptance:

1. `uv run pytest -q tests/entrypoints/api tests/infrastructure/queue tests/application/dispatcher` passes.
2. Any remaining skipped tests include a reason tied to an explicit deferred scope.
3. The test suite fails if API ingress ever emits non-v3 payloads again.

---

## Workflow closure requirements

### Triage closure

Current state: ACK only.

Required v0.1 alpha behavior:

1. On `issues.opened`, select 1-3 labels from configured labels and existing
   repo labels.
2. Add a priority label (`priority/P0` through `priority/P3`) when confidence
   is adequate.
3. If issue looks like a bug with enough reproduction info, run a bounded
   read/test reproduce path in sandbox and post concise evidence.
4. If issue is spam/question/no repro, avoid sandbox and post a low-cost
   clarification or label-only result.

Non-goals for this closure:

1. Issue dedup.
2. Automatic close.
3. Maintainer-specific triage policy learning.

Acceptance:

1. Unit tests cover label/priority parsing and threshold behavior.
2. E2E-style test proves `issues.opened` can add labels and post one comment.
3. No triage path can write repository files.

### Review closure

Current state: closest to PRD target.

Required v0.1 alpha behavior:

1. Keep structured findings and severity filtering.
2. Preserve advisory behavior: never emit `REQUEST_CHANGES`, never merge.
3. Ensure incremental review metadata is connected from worker completion to
   future `pull_request.synchronize` events.
4. Add bot-output secret scan before review body/comments leave the process.

Acceptance:

1. Review tests cover zero findings -> `APPROVE`.
2. Review tests cover medium/high findings -> `COMMENT`.
3. Fork PR default-deny is tested through preflight and not bypassed by direct
   handler invocation.
4. Secret-like text in a finding is redacted or blocks egress.

### Fix closure

Current state: handler is mostly complete, production entry wiring is not.

Required v0.1 alpha behavior:

1. Issue assignment to the bot triggers fix only for authorized actors.
2. Worker provisions sandbox, clones the correct ref, and passes
   `SandboxedHandle` into `maybe_run_fix`.
3. DeepAgents fix uses sandbox tools only; generated changes stay inside the
   cloned workspace.
4. Successful fix creates branch, pushes, opens PR, and posts PR URL.
5. Failed tests produce a bounded test-output comment.
6. Cancellation checkpoints run after slow I/O, after agent loop, after branch
   creation, and after push.

Deferred from v0.1 alpha unless already cheap:

1. CI failure self-fix up to 3 times.
2. Persistent warm sandbox cache.
3. Advanced branch naming by issue slug.

Acceptance:

1. A fake-sandbox worker integration test opens a fake PR from an issue
   assignment.
2. Unauthorized actor assignment is blocked before sandbox creation.
3. Cancellation signal stops before push when raised at the post-agent
   checkpoint.
4. No token appears in logs when clone or push fails.

### Chat closure

Current state: help/cancel parse exists; freeform answer has no tools.

Required v0.1 alpha behavior:

1. `@openbot help` returns structural help.
2. `@openbot stop|cancel|停|取消` records cancellation and replies immediately.
3. Freeform chat can answer repo-grounded questions using read-only tools.
4. Chat cannot write files, run mutating shell commands, create branches, open
   PRs, label issues, or merge.
5. Chat clearly refuses or redirects action requests that require state changes.

Tool allowance for v0.1 closure:

| Tool | Allowed | Notes |
|---|---|---|
| `read_file` | yes | Repo-relative only. |
| `list_files` / `glob` | yes | Bounded output. |
| `grep` | yes | Bounded output and path allowlist. |
| `shell_readonly` | optional | Only if argv allowlist is explicit. |
| `web_fetch` | optional | Can be deferred if SSRF controls are not ready. |
| `write_file` | no | Fix workflow only. |
| `shell_write` | no | Fix workflow only through sandbox run policy. |
| `gh_pr_create` / `gh_pr_merge` | no | Never chat. |

Acceptance:

1. Chat responder registers read-only tools in tests.
2. A request like "open a PR" receives an explanatory refusal or points the
   user to assign the issue to the bot.
3. A request like "where is config loaded?" can inspect repo files and answer
   with file paths.

---

## Cross-cutting closure requirements

### Cost caps

Current state:

1. `cost_meter` schema exists.
2. `complete(...)` records cost for its own LiteLLM calls.
3. Budget middleware checks monthly repo soft cap and global hard kill before
   workflow start.
4. DeepAgents responder calls do not clearly flow through the cost-meter
   wrapper.

Required v0.1 alpha behavior:

1. Every LLM call that can bill the user records cost or records a degraded
   cost-status row.
2. Per-task budget is checked inside long-running agent execution before
   additional LLM/tool steps.
3. Monthly soft cap blocks repo workflows with one bounded notification.
4. Global hard kill stops dequeue or causes every worker dispatch to reject
   before spending further LLM tokens.

Acceptance:

1. Tests prove a task over per-task cap stops before the next LLM/tool step.
2. Tests prove monthly/global caps use only `RECORDED` cost rows for strict
   arithmetic.
3. Unknown pricing is visible in audit and does not masquerade as free spend.

### Cancellation

Current state:

1. Kill switch preflight exists.
2. Cancel label preflight exists.
3. Cancel comment records a Redis signal.
4. Fix has explicit checkpoints at several critical points.

Required v0.1 alpha behavior:

1. Long-running review/fix/chat agents must check cancellation at bounded
   intervals.
2. Cancel label must stop future work before sandbox creation when present at
   ingress.
3. Comment cancellation must signal the active `run_id`, not only the delivery
   task id, when a prior run exists.

Acceptance:

1. A superseded run receives a cancellation signal and stops before write-back.
2. `OPENBOT_KILL_SWITCH=true` prevents LLM calls in worker execution.
3. Cancel label on issue/PR prevents sandbox creation.

### Output egress scanning

Current state:

Developer-side TruffleHog hooks and CI exist. Bot-authored egress scanning is
not yet a clearly enforced adapter boundary.

Required v0.1 alpha behavior:

1. All bot-authored GitHub text passes through one output-safety function
   before calling the adapter:
   - issue/PR replies;
   - PR review body;
   - PR review inline comments;
   - PR title/body if model-influenced;
   - failed test output snippets.
2. Verified secret hits are redacted or block egress with a safe fallback.
3. The scanner is timeout-bounded and failure-safe.

Acceptance:

1. A fake token in a model finding does not reach `adapter.create_pr_review`.
2. A fake token in fix test output does not reach `adapter.reply`.
3. Scanner timeout emits a safe fallback and audit event.

### Documentation alignment

Current drift:

1. PRD names Modal as the sandbox; code and deploy docs are Daytona-first.
2. PRD names Claude Opus/Sonnet; code currently defaults to `anthropic/GLM-5.1`.
3. README still says "not yet runnable end-to-end", which is true today but
   should become a release-gated status rather than stale copy.

Required v0.1 alpha behavior:

1. Decide final v0.1 sandbox provider name and update PRD, README, app.json,
   deploy docs, and `.env.example`.
2. Decide final model default and update PRD/config docs or code.
3. Add a concise "current alpha status" section that points to this spec or its
   follow-up plan until closure is complete.

Acceptance:

1. No doc claims Modal while production config requires Daytona, or vice versa.
2. No doc claims Claude defaults while `primary_model_for(...)` defaults to
   GLM, or vice versa.
3. `rg "Modal|Daytona|claude-opus|claude-sonnet|GLM-5.1"` shows intentional,
   non-conflicting wording.

---

## Implementation order

This is the recommended order for the follow-up implementation plan.

| Order | Workstream | Why first |
|---|---|---|
| 1 | Queue contract closure | Without this, webhook events cannot reliably reach handlers. |
| 2 | Stale test migration | Prevents working against deleted APIs and false confidence. |
| 3 | Worker sandbox injection | Required for fix to be more than a degrade reply. |
| 4 | Review stabilization | Closest workflow to done; good first dogfood signal. |
| 5 | Chat read-only grounding | Small capability addition with clear safety boundary. |
| 6 | Triage real behavior | Larger product behavior, depends on sandbox/tool/runtime decisions. |
| 7 | Runtime cost/cancel hooks | Should be built once in the DeepAgents runtime, then inherited by workflows. |
| 8 | Output egress scanning | Must wrap all write-back paths before alpha. |
| 9 | Docs alignment | Should land with the real decisions, not before. |

The queue, tests, and worker sandbox wiring are not optional polish. They are the
minimum needed to make the product path executable.

---

## Alpha readiness definition

OpenBot v0.1 alpha is dogfoodable when all of these are true:

1. `make check` passes locally.
2. `make -C evals test` passes locally.
3. A local signed webhook smoke can exercise:
   - issue opened -> triage visible action;
   - PR opened -> review visible action;
   - issue assigned to bot -> fake or real sandbox fix path;
   - `@openbot help`, `@openbot stop`, and one repo-grounded freeform chat.
4. Worker consumes exactly the payload type the webapp produces.
5. Fix can run with a configured sandbox backend and degrades clearly without
   one.
6. Per-task, monthly, and global budget gates have tests at the point they are
   enforced.
7. Bot-authored output cannot leak a synthetic secret in tests.
8. README and PRD no longer contradict the actual sandbox/model defaults.

---

## Non-goals for this closure

The following stay out of v0.1 product closure unless they are already needed
to remove a blocker:

1. Linear adapter.
2. Slack or Discord adapters.
3. Web dashboard.
4. Issue dedup.
5. Community plugin PR workflow.
6. PyPI plugin sandbox.
7. Optional hosted multi-tenant OpenBot Cloud.
8. Full production Docker Hub release automation if local/dev deployment is
   still not dogfoodable.

---

## Follow-up plan requirements

The implementation plan generated from this spec must:

1. Start with tests that fail on the current queue contract mismatch.
2. Avoid restoring deleted compatibility APIs like `run_dispatch` unless there
   is a real production caller.
3. Keep changes narrow and commit-sized:
   - queue contract;
   - test migration;
   - worker sandbox factory;
   - workflow closures;
   - runtime budget/cancel hooks;
   - output scanning;
   - docs alignment.
4. Use `uv run pytest` for repo tests.
5. Use fake adapters/fake sandboxes for fast tests before attempting live
   Daytona or GitHub integration.
