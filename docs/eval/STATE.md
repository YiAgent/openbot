# Eval Task State

**Last updated**: 2026-05-15
**Active branch**: eval/E2-rollup (E2 safety stream complete; E1 baseline-closure cleanup in progress)
**Next focus**: close E1 partials truthfully; next numbered task after closure is E2-T01 — lock real Martian dataset (https://github.com/withmartian/code-review-benchmark)

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
- [x] E1-T05 — 2026-05-15 — handoff: handoffs/E1-T05.md
- [x] E1-T06 — 2026-05-15 — handoff: handoffs/E1-T06.md  (`deepagents_baseline` provider complete; future `openbot_prod` provider remains separate work)
- [x] E1-T07 — 2026-05-15 — handoff: handoffs/E1-T07.md
- [x] E2-T12 — 2026-05-15 — handoff: handoffs/E2-T12.md  (prompt_injection_v1, 24 cases, 6 categories)
- [x] E2-T13 — 2026-05-15 — handoff: handoffs/E2-T13.md  (safety scorer with breach-category attribution)
- [x] E2-T14 — 2026-05-15 — handoff: handoffs/E2-T14.md  (redteam task: 21/24 fail-safe; 3 context-blind FPs)
- [x] E2-T16 — 2026-05-15 — handoff: handoffs/E2-T16.md  (failure_category enum validation)

## Partial
<!-- Format: <id> — open acceptance gap -->
- E1-T04 — artifact surface exists, but `export_artifact()` still needs the real implementation + live LangSmith round-trip.
- E1-T08 — synthetic smoke path runs, but full LangSmith trace / sample metadata / real artifact closure is not yet complete.
- E1-T09 — local `.eval` → markdown export works, but LangSmith-source export remains open.
- E1-T10 — routing code is present, but manual internal-vs-public project verification is still open.

## Blocked
<!-- Format: <id> — blocker note -->
- _(none)_

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
