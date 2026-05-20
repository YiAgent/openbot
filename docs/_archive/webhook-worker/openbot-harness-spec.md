# OpenBot · Harness Spec (v0.1 Week 2+) — Archived

> Status: **Archived — completed 2026-05-20.** All M1–M12 input-side modules
> shipped, all 9 §7 acceptance demos pass in `tests/e2e/test_spec_demos.py`,
> and the work has been extended by webhook-worker-layering F1/F2/F3.
>
> - Original draft date: 2026-05-15
> - Full original (700 lines) preserved in git history before this archive commit.
> - Companion archived doc: [`input-side-completeness-slice-e.md`](./input-side-completeness-slice-e.md).
> - Forward-evolution plans now archived under
>   [`docs/superpowers/plans/`](../../superpowers/plans/) (search
>   `2026-05-20-webhook-worker-layering-*`).

This file is kept as the historical record of the input-side harness contract.
**Do not edit as if it were a live spec** — instead, write a new slice doc
referencing the current code paths under `openbot/` and link back here for
context.

---

## What shipped (M1–M12 status)

| # | Spec module | Final code path | Status |
|---|---|---|---|
| M1 | Config loader | `openbot/infrastructure/config_loader.py` | ✅ |
| M2 | Router + `derive_task_id` | `openbot/application/router.py` | ✅ |
| M3 | Pre-flight middleware framework | `openbot/application/middleware/preflight.py` | ✅ |
| M4 | Cancel (kill switch / label / comment) | `openbot/application/middleware/cancel.py` | ✅ |
| M5 | Budget enforcement | `openbot/application/middleware/budget.py` | ✅ |
| M6 | Rate limiter (Redis) | `openbot/application/middleware/rate_limit.py` + `openbot/infrastructure/persistence/rate_limiter_redis.py` | ✅ |
| M7 | Fork-PR + actor-role gate | `openbot/application/middleware/security.py` | ✅ |
| M8 | Prompt-injection wrapper | `openbot/infrastructure/llm/sanitize.py` + lint guard `tests/application/test_no_raw_user_input.py` | ✅ |
| M9 | AuditStart + lifecycle | `openbot/application/middleware/audit.py` + `openbot/application/use_cases/_lifecycle.py` | ✅ |
| M10 | Redis Stream queue + worker | `openbot/infrastructure/queue/` + `openbot/entrypoints/worker/__main__.py` | ✅ |
| M11 | Chat command parser | `openbot/application/use_cases/chat_parser.py` | ✅ |
| M12 | Workflow stubs | `openbot/application/use_cases/{triage,review,fix,chat}.py` | ✅ stubs; chat now real (see below) |

**Locked chain order** (single source of truth: `openbot/application/dispatcher.py::build_preflight_chain`):

```
sanitize_inputs → kill_switch → feature_toggle →
cancel_label → cancel_comment → fork_pr_gate →
actor_role → rate_limit → budget → audit_start → handler
```

Reorder requires a spec amendment plus an update to
`tests/application/middleware/test_chain_order.py`.

### Path drift fixed by archive

The original spec used pre-hexagonal paths (`openbot.workflows/*`,
`openbot.middleware/*`, `webapp.py:234`). The table above reflects the
post-restructure layout (`domain/`, `application/`, `infrastructure/`,
`core/`, `entrypoints/`) from PR #51–#54.

---

## §7 acceptance demos — where they live

All 9 end-to-end demos run in CI as `tests/e2e/test_spec_demos.py`:

| # | Demo | Test |
|---|---|---|
| 1 | Issue → triage ACK + audit STARTED/COMPLETED | `test_demo_01_issue_opens_triage_acks` |
| 2 | PR → review stub ACK + audit | `test_demo_02_pr_opens_review_stub_acks` |
| 3 | Bot assignee → fix stub ACK + audit | `test_demo_03_bot_assigned_fix_stub` |
| 4 | `@openbot ...` → chat stub ACK + audit | `test_demo_04_at_openbot_chat_ack` |
| 5 | `cancel-openbot` label → BLOCKED + REJECTED audit | `test_demo_05_cancel_label_blocks` |
| 6 | `OPENBOT_KILL_SWITCH=true` → all workflows BLOCKED | `test_demo_06_kill_switch_env_blocks` |
| 7 | Fork PR off by default; `/ok-to-test` opens | `test_demo_07_fork_pr_default_off_ok_to_test_opens` |
| 8 | Rate-limited user sees one comment | `test_demo_08_rate_limited_user_sees_single_comment` |
| 9 | Worker `kill -9` then restart re-delivers | `test_demo_09_worker_restart_does_not_drop_message` |

Total test count at archive time: **828 passing** (vs. spec target ~250).

---

## What was extended after archive: webhook-worker-layering F1/F2/F3

The input-side harness was the *floor*. The dispatcher layer above it grew
substantially after Slice E landed:

| Slice | What | Commit / PR | Plan |
|---|---|---|---|
| **F1** | TaskSpec v3 contract + `decide_and_enqueue` (D1–D9 webhook async segment) + worker v3 routing | `d2aa7c3` / PR #60 | `docs/superpowers/plans/2026-05-20-webhook-worker-layering-f2-part1.md` (later parts cover F2/F3) |
| **F2** | Direct-action short-circuit (D11/D12): canned replies + label-only paths bypass the LLM | `dfcfaed` / PR #61 | `2026-05-20-webhook-worker-layering-f2-part2.md` |
| **F3** | D10 LLM classifier + incremental review (`is_incremental` / `is_force_push` / `stages_to_run`) + `last_reviewed_sha` wiring | `dfcfaed` / PR #61 | `2026-05-20-webhook-worker-layering-f3-part{1,2,3}.md` |
| F-refactor | Dispatcher simplification + 18 E2E demos `test_decide_and_enqueue_demos.py` | `e5a84d1` / PR #62 | — |

New subsystems that did **not** exist in this spec:

- `openbot/dispatcher/` — webhook async segment (D1–D9 preflight, D10 classifier,
  D11/D12 direct-action, TaskSpec build, enqueue).
- `openbot/application/state/` — `classifier`, `cancellation`, `resource_lock`,
  `runs_repo`. The `task_runs` table and per-resource Redis lock land here.
- `openbot/application/ports/` — 11 hexagonal ports (channel-adapter, audit-log,
  rate-limiter, config-loader, queue, etc.) introduced in PR #52.

When reading this spec, treat M1–M12 as *the input-side floor*. The current
runtime is wider: `decide_and_enqueue` runs the M3 chain **then** classifies,
short-circuits, or enqueues a TaskSpec v3 for the worker.

---

## chat workflow agent loop — DeepAgent landed

Spec §1.2 / §8 listed "LangGraph DeepAgent loop" as a v0.2 non-goal. Status as of
this archive:

- ✅ **chat**: `openbot/infrastructure/agents/deepagents_chat.py` is wired into
  `openbot/application/use_cases/chat.py`. `@openbot ...` mentions get a real
  agent response (not a stub) after the M3 preflight chain passes.
- ❌ **review / fix**: still §7 demo stubs (`maybe_run_review` and
  `maybe_run_fix` ACK + audit only). Planned next as a separate slice — see
  the planning task in the active session and (forthcoming)
  `docs/superpowers/plans/2026-05-20-review-fix-deepagent.md`.

---

## Locked decisions still in force (§9 of original)

These four are still authoritative — point future slices at this file when
reusing them:

1. **§9.1 `task_id` derivation** — `hashlib.sha256(f"{channel}|{repo}|{delivery_id}").hexdigest()[:32]`.
   Implementation: `openbot/domain/identifiers.py::derive_task_id`. Determinism
   is property-tested.
2. **§9.2 announce-once policy** — Redis `SET NX EX` guarded comments; matrix of
   which middlewares reply / which stay silent unchanged. Implementation:
   `openbot/application/middleware/preflight.py::announce_once`.
3. **§9.3 worker model** — single process + `asyncio.gather` over N consumers
   (default 4, env `OPENBOT_WORKER_CONCURRENCY`). Implementation:
   `openbot/entrypoints/worker/__main__.py`.
4. **§9.4 cancel-comment always replies** — `@openbot stop|cancel|停|取消` never
   goes through `announce_once`; user gets a confirmation every time.
   Implementation: `openbot/application/middleware/cancel.py::CancelCommentMiddleware`.

---

## Still deferred to v0.2 (§8 unchanged)

These were explicit non-goals in the spec and remain deferred:

- LangGraph DeepAgent loop for **review / fix** (chat done — see above).
- Modal sandbox integration (`SandboxBackend` ABC exists in `evals/sandboxes/`
  for benchmarks; workflow side is still TODO).
- Trufflehog comment-egress scanning.
- LangSmith trace at the workflow level (currently only at LiteLLM).
- pgvector / issue deduplication.
- `openbot audit` CLI — **picked up in this same session**; see
  `openbot/entrypoints/cli/audit.py`.
- `.openbot/config.yaml` high-risk-field `config-approved` label gate.
- LinearAdapter.

---

## References

- Live PRD (still authoritative): [`../../prd/openbot-prd.md`](../../prd/openbot-prd.md).
- Eval PRD: [`../../prd/openbot-eval-prd.md`](../../prd/openbot-eval-prd.md).
- Config example: [`../../prd/openbot-config-example.yaml`](../../prd/openbot-config-example.yaml).
- Companion archive: [`./input-side-completeness-slice-e.md`](./input-side-completeness-slice-e.md).
- Test plan archive: [`./webhook-worker-test-plan.md`](./webhook-worker-test-plan.md).
