"""SWE-bench Verified — PRD §3.1 (``fix_swe_bench`` eval surface).

Architecture:

- Solver runs the OpenBot fix workflow in a Daytona sandbox cloned at
  ``base_commit`` (see :mod:`evals.solvers.fix`).
- The "scorer" is :func:`evals.data._predictions.prediction_exporter`,
  which validates the agent's :class:`SweBenchPrediction` against the
  official schema and appends it to
  ``evals/outputs/fix_swe_bench/<run-label>.predictions.jsonl``.
- **Actual grading happens offline** via the SWE-bench Docker harness:

    python -m swebench.harness.run_evaluation \\
        --dataset_name princeton-nlp/SWE-bench_Verified \\
        --predictions_path evals/outputs/fix_swe_bench/<run>.predictions.jsonl \\
        --max_workers 8 --run_id openbot-{date}

Run::

    doppler run --project openbot --config dev -- \\
        uv run inspect eval 'evals/tasks/fix_swe_bench.py' --limit 5
"""

from __future__ import annotations

from inspect_ai import Task, task

from evals.data import FIX
from evals.solvers.fix import openbot_fix_solver


@task
def fix_swe_bench() -> Task:
    """SWE-bench Verified with the OpenBot fix solver.

    Produces ``predictions.jsonl`` in the official SWE-bench format.
    Real grading is offline via the SWE-bench Docker harness.
    """
    return FIX.build_task(solver=openbot_fix_solver())
