# Eval Task Resume Protocol

When the user asks to continue eval work — typical triggers: `next eval task`, `继续下一个 eval 任务`, `resume eval` — follow this protocol exactly. **One task per session.** Do not batch.

## Steps

1. **Read [`STATE.md`](./STATE.md).** The `Next task:` line is authoritative. If it says `(none)` or all tasks are Done, stop and tell the user.
2. **Read the matching section** of [`task-list.md`](./task-list.md) (grep for `### <id>`). Note its `Deps:`, `Deliverables:`, `AC:`, `PRD ref:`, `Effort:`.
3. **Read each dependency's handoff** at `docs/eval/handoffs/<dep-id>.md`. Pay attention to "Pointers for next task" and "Open follow-ups" sections — that is the deliberate hand-off surface.
4. **Confirm with the user** before writing code:
   > Next task is **`<id>` — `<title>`**. Deps satisfied via handoffs: `<list>`. Proceed?
5. **Implement** the smallest slice that satisfies `Deliverables:` + `AC:`. Honor [`CLAUDE.md`](../../CLAUDE.md) locked boundaries (Modal / LangSmith / Inspect AI; GitHub-only v0.1; no Slack/Discord/Linear; no LLM-behavior asserts in `tests/`).
6. **Verify**:
   - `make check` (per CLAUDE.md — runs fmt-check + lint + test)
   - any AC-specific command the task lists (e.g. `python scripts/validate_langsmith_run.py --help`)
   - capture the **verbatim output** for the handoff
7. **Write `docs/eval/handoffs/<id>.md`** from [`handoffs/_template.md`](./handoffs/_template.md). Include:
   - actual command output (trimmed, but not paraphrased)
   - AC checklist with each verbatim AC bullet from `task-list.md` checked off
   - decisions taken, with PRD § references
   - concrete pointers for downstream tasks
8. **Update [`STATE.md`](./STATE.md)**:
   - Append under Done: `- [x] <id> — <YYYY-MM-DD> — handoff: handoffs/<id>.md`
   - Recompute `Next task:` per the algorithm at the bottom of `STATE.md`
   - Bump `Last updated`
   - Update `Active branch:` if a new milestone is starting
9. **Commit** code + handoff + `STATE.md` together on branch `eval/<milestone>`:
   - branch name: `eval/E0`, `eval/E1`, …
   - first task of a milestone: `git checkout -b eval/<m>` from `main`
   - commit message format: `<type>(<id>): <one-line goal>` — e.g. `feat(E0-T02): scaffold evals/ directory`
   - never `--no-verify`; if pre-commit fails, fix the cause and try again
10. **Milestone boundary**: if the just-finished task is the last of its milestone (no remaining non-🕒 tasks with the same `E{m}-` prefix):
    - Instruct the user to open a PR `eval/<m> → main`
    - Tell them the next session will branch `eval/<next m>` from `main` after the PR merges
    - Set `Active branch:` in `STATE.md` to the next milestone's branch as a hint
11. **Final message** must end with:
    > Done. Run `/clear`, then say `next eval task` to continue.

## Anti-patterns

- ❌ Doing two tasks in one session "since they're small" — defeats the purpose of context isolation.
- ❌ Paraphrasing test output in the handoff — unverifiable; future sessions can't trust it.
- ❌ Skipping the `Deps:` handoffs to "save context" — they exist precisely to replace lost in-conversation memory.
- ❌ Implementing more than `Deliverables:` requires — future tasks own that scope.
- ❌ Mixing handoff + code commits with unrelated changes.

## Edge cases

- **Task spec is ambiguous**: prefer minimum interpretation matching its `AC:` exactly. Capture the ambiguity in the handoff "Decisions made" with a follow-up note.
- **Test fails after implementation**: do NOT update STATE.md or commit. Either fix or write a `❌ blocked` handoff and mark the task Blocked in STATE.md; tell the user.
- **Discovering the task should depend on something not listed**: still complete the visible scope; record the missing dep in "Open follow-ups" + propose adding it to `task-list.md` in a separate edit.
- **A 🕒 deferred task somehow becomes `Next task`**: stop, tell the user, and re-pick.
