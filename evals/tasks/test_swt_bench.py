"""SWT-Bench Verified — PRD §3.1 (``test_swt_bench`` eval surface).

The solver emits an explicit unsupported-capability prediction until OpenBot
has a real test-generation capability (see :mod:`evals.solvers.test_generation`).
Grading is offline via the SWT-Bench Docker harness.

Run::

    doppler run --project openbot --config dev -- \\
        uv run inspect eval 'evals/tasks/test_swt_bench.py' --limit 5
"""

from __future__ import annotations

from inspect_ai import Task, task

from evals.data import SWT
from evals.solvers.test_generation import openbot_test_generation_solver


@task
def test_swt_bench() -> Task:
    """SWT-Bench Verified with the OpenBot test-generation solver.

    Until test generation is implemented, each sample emits an empty
    SwtBenchPrediction. The exporter validates the schema and writes the
    JSONL the upstream Docker harness consumes for real pass@1.
    """
    return SWT.build_task(solver=openbot_test_generation_solver())
