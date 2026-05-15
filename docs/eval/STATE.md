# Eval Task State

**Last updated**: 2026-05-15
**Active branch**: eval/E2-rollup (Martian dataset locked; baseline run E2-T02 is next)
**Next task**: _(E2 stream paused — see "Blocked" below; resumes on either cost-budget approval for E2-T02 or Modal+fix-workflow scaffolding for E2-T07. Deferred E2-T03..T06 / T09..T10 wait on PRD §4.0 internal-data gates.)_

## Status legend

- **Done** — implementation exists and the original acceptance criteria are actually satisfied.
- **Partial** — useful implementation exists, but one or more original acceptance criteria are still open.
- **Deferred** — intentionally postponed by product decision; do not treat as blocked.

## Done
<!-- chronological, append-only. Format: - [x] <id> — <YYYY-MM-DD> — handoff: handoffs/<id>.md -->
- [x] E0-T01 — 2026-05-15 — handoff: handoffs/E0-T01.md
- [x] E0-T02 — 2026-05-15 — handoff: handoffs/E0-T02.md
- [x] E0-T03 — 2026-05-15 — handoff: handoffs/E0-T03.md
- [x] E0-T04 — 2026-05-15 — handoff: handoffs/E0-T04.md
- [x] E0-T05 — 2026-05-15 — handoff: handoffs/E0-T05.md  (E0 milestone complete)
- [x] E1-T01 — 2026-05-15 — handoff: handoffs/E1-T01.md
- [x] E1-T02 — 2026-05-15 — handoff: handoffs/E1-T02.md
- [x] E1-T03 — 2026-05-15 — handoff: handoffs/E1-T03.md
- [x] E1-T04 — 2026-05-15 — handoff: handoffs/E1-T04.md  (real artifact export + 3/3 live LangSmith tests)
- [x] E1-T05 — 2026-05-15 — handoff: handoffs/E1-T05.md
- [x] E1-T06 — 2026-05-15 — handoff: handoffs/E1-T06.md  (`deepagents_baseline` provider complete; future `openbot_prod` provider remains separate work)
- [x] E1-T07 — 2026-05-15 — handoff: handoffs/E1-T07.md
- [x] E1-T09 — 2026-05-15 — handoff: handoffs/E1-T09.md  (local + direct-LangSmith summary paths)
- [x] E1-T10 — 2026-05-15 — handoff: handoffs/E1-T10.md  (dual-project routing verified live)
- [x] E2-T12 — 2026-05-15 — handoff: handoffs/E2-T12.md  (prompt_injection_v1, 24 cases, 6 categories)
- [x] E2-T13 — 2026-05-15 — handoff: handoffs/E2-T13.md  (safety scorer with breach-category attribution)
- [x] E2-T14 — 2026-05-15 — handoff: handoffs/E2-T14.md  (redteam task: 21/24 fail-safe; 3 context-blind FPs)
- [x] E2-T16 — 2026-05-15 — handoff: handoffs/E2-T16.md  (failure_category enum validation)
- [x] E2-T01 — 2026-05-15 — handoff: handoffs/E2-T01.md  (Martian benchmark locked to upstream `807d469`, 50 PR JSONL pinned)
- [x] E2-T15 — 2026-05-15 — handoff: handoffs/E2-T15.md  (PRD §9 thresholds module + `compare_runs.py` PR-comment renderer)

## Partial
<!-- Format: <id> — open acceptance gap -->
- E1-T08 — synthetic smoke path, artifacts, LangSmith sync, and validation now run end-to-end; cost remains provider-dependent and is explicitly reported as `unavailable` when the provider does not emit price data.

## Blocked
<!-- Format: <id> — blocker note -->
- **E2-T02** — Martian 全量 run + baseline. Blocker: needs LLM cost-budget approval to spend tokens against the locked 50-PR dataset, and a price-emitting provider (E1-T08 noted `glm-5.1` does not emit price metadata — Claude or OpenAI required for the recorded baseline row). Unblocks E2-T15's consumer side and downstream G1/G2 regression gating.
- **E2-T07** — Fix solver + Modal sandbox 集成. Blocker: (a) Modal credentials not provisioned in Doppler config; (b) the OpenBot `fix` workflow does not yet exist in the repo skeleton (per `CLAUDE.md`, current v0.1 Week 1 surface is `webapp.py + config.py + events.py + adapters/` only). Cannot wrap a workflow that hasn't been written.
- **E2-T08** — Patch tests scorer. Blocker: depends on E2-T07's `openbot_fix` solver producing patches to score.
- **E2-T11** — `swe_bench_lite` 接入. Blocker: depends on E2-T07 + docker sandbox config (PRD §6 双 sandbox).

## Scope notes
- Internal-data-dependent tasks are DEFERRED (see [`task-list.md` §"范围调整"](./task-list.md) and [`openbot-eval-prd.md` §4.0](../prd/openbot-eval-prd.md#40-范围调整--internal-data-dependent-suite-全部-deferred2026-05-15-锁定)). 🕒 tasks are SKIPPED when picking next.
- Locked boundaries per [`CLAUDE.md`](../../CLAUDE.md): Modal sandbox, LangSmith observability, Inspect AI runner, GitHub-only v0.1 channel.

## How the `Next task` pointer is set
The handoff step for task X writes the next pointer by:
1. Filter [`task-list.md`](./task-list.md) for tasks whose every `Deps:` entry is in **Done** above.
2. Drop 🕒 deferred tasks (see Scope notes).
3. Pick the lowest `E{m}-T{nn}` ordinal among the remaining ready candidates.
4. Overwrite the `Next task:` line at the top of this file.

If multiple ready candidates exist and you want a non-default order, manually edit `Next task:` before the next session — the resume protocol trusts this file.

## Manual overrides
- **Skip a task**: add it under Done with `(skipped: <reason>)` and recompute `Next task`.
- **Force a different next**: edit `Next task:` directly; commit the change with the upcoming work.
- **Mark blocked**: move from "Next task" candidates into the Blocked list with a one-line cause.
