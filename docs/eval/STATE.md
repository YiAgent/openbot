# Eval Task State

**Last updated**: 2026-05-15
**Active branch**: eval/E1-T01..T10 (Graphite stack on eval/E0)
**Next task**: E1-T06 (will land as ❌ blocked — openbot.workflows.review missing)

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

## Blocked
<!-- Format: <id> — blocker note -->
- E1-T04 — needs live LANGSMITH_API_KEY for AC integration test; skeleton only. See handoffs/E1-T04.md.

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
