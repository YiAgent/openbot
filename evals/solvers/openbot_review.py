"""Inspect solver wrapping the real OpenBot review workflow — PRD §4.1 / §6.2.

**E1-T06 SKELETON ONLY.** Blocked on two unmet preconditions:

  1. `openbot.workflows.review.run(...)` does not exist yet (v0.1 Week 1
     skeleton in `openbot/` only ships webapp / config / events / adapters).
  2. E1-T04 artifact export is still ❌ blocked (this solver is supposed to
     attach per-sample patches via `evals.common.artifacts.export_artifact`).

The solver factory below registers the Inspect `@solver`-shaped function so
downstream tasks can `from evals.solvers.openbot_review import openbot_review_solver`,
but invocation raises `NotImplementedError` until both blockers clear.

See `docs/eval/handoffs/E1-T06.md` for the unblock checklist.
"""

from __future__ import annotations

from typing import Any


def openbot_review_solver() -> Any:
    """Return an Inspect solver that feeds PR diffs to the real review workflow.

    Output (when unblocked) will normalize findings to::

        {"file": str, "line": int | None, "body": str, "severity": Literal["low", "medium", "high"]}

    Currently a stub: raises NotImplementedError at construction time, so an
    Inspect task file that imports + invokes this will fail loudly rather than
    silently emit a no-op solver.
    """
    raise NotImplementedError(
        "E1-T06 review solver is blocked: needs `openbot.workflows.review.run(...)` "
        "(v0.1 Week 1 skeleton doesn't ship it) and E1-T04 artifact export. "
        "See docs/eval/handoffs/E1-T06.md."
    )
