"""Solver-provider registry for review evals.

`deepagents_baseline` is the durable comparator that exists today.
`openbot_prod` is intentionally reserved for the future production workflow so
both providers can be evaluated on the same task / dataset / scorer surface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, TypeAlias

from evals.solvers.openbot_review import deepagents_baseline_review_solver

ReviewSolverId: TypeAlias = Literal["deepagents_baseline", "openbot_prod"]
ReviewSolverFactory: TypeAlias = Callable[[], object]


def get_review_solver(solver_id: str) -> ReviewSolverFactory:
    """Return the solver factory for a known review provider id."""
    if solver_id == "deepagents_baseline":
        return deepagents_baseline_review_solver
    if solver_id == "openbot_prod":
        raise NotImplementedError(
            "Review solver provider 'openbot_prod' is reserved until "
            "openbot.workflows.review.run(...) ships."
        )
    raise ValueError(
        f"Unknown review solver_id {solver_id!r}. "
        "Expected one of: 'deepagents_baseline', 'openbot_prod'."
    )
