"""SWT-bench test-generation solver — calls the openbot reproduce agent.

The reproduce agent writes a pytest test file that triggers the reported
failure, runs it to confirm it fails, then emits the git unified diff via
``git_diff()``.  This solver maps that diff to ``SwtBenchPrediction.model_patch``
so the upstream Docker harness can grade it offline.

Architecture mirrors the other three solvers (review / fix / chat):
  1. Obtain a sandbox factory from the openbot product sandbox layer.
  2. Delegate the full lifecycle to ``run_test_generation_sample`` — same
     pattern as ``run_fix_sample`` (clone → agent → close).
  3. Store a ``SwtBenchPrediction`` dict in ``state.metadata["prediction"]``
     for the ``prediction_exporter`` scorer.
"""

from __future__ import annotations

import logging

from evals.data._predictions import SwtBenchPrediction, empty_swt_prediction
from openbot.evaluation import run_test_generation_sample

logger = logging.getLogger(__name__)


def openbot_test_generation_solver(*, model: str | None = None):
    """Inspect ``@solver`` — calls the repro agent to write a failing test."""
    try:
        from inspect_ai.solver import solver
    except ImportError:

        def solver(fn):
            return fn

    @solver
    def _solver():
        async def _run(state, _generate):
            instance_id = str(state.sample_id) if state.sample_id is not None else "anon"
            metadata = state.metadata or {}

            repo = metadata.get("repo", "")
            base_sha = metadata.get("base_commit", "")
            clone_url = metadata.get("clone_url") or (
                f"https://github.com/{repo}.git" if repo else ""
            )
            problem_statement = metadata.get("problem_statement", "")
            # SWT-bench rows carry the issue number; fall back to 0 for safety.
            issue_number = int(metadata.get("issue_number", 0)) or 0

            sandbox_factory = metadata.get("sandbox_factory")

            if not sandbox_factory:
                logger.warning(
                    "swt_bench_solver_no_sandbox_factory instance_id=%s — "
                    "emitting empty prediction",
                    instance_id,
                )
                prediction = empty_swt_prediction(
                    instance_id=instance_id,
                    model_name_or_path="openbot",
                )
                state.metadata["prediction"] = prediction.model_dump()
                state.metadata["prediction_json"] = prediction.model_dump_json()
                state.output.completion = f"NO_SANDBOX: {instance_id}"
                return state

            try:
                outcome = await run_test_generation_sample(
                    instance_id=instance_id,
                    repo=repo,
                    issue_number=issue_number,
                    problem_statement=problem_statement,
                    base_sha=base_sha,
                    clone_url=clone_url,
                    sandbox_factory=sandbox_factory,
                )
                model_patch = outcome.repro_artifact or ""
                prediction = SwtBenchPrediction(
                    instance_id=instance_id,
                    model_name_or_path="openbot",
                    model_patch=model_patch,
                )
                state.metadata["prediction"] = prediction.model_dump()
                state.metadata["prediction_json"] = prediction.model_dump_json()
                state.metadata["repro_status"] = outcome.status
                state.output.completion = f"status={outcome.status} patch_len={len(model_patch)}"
            except Exception as exc:
                logger.exception(
                    "swt_bench_solver_error instance_id=%s error=%s",
                    instance_id,
                    exc,
                )
                prediction = empty_swt_prediction(
                    instance_id=instance_id,
                    model_name_or_path="openbot",
                )
                state.metadata["prediction"] = prediction.model_dump()
                state.metadata["prediction_json"] = prediction.model_dump_json()
                state.metadata["solver_error"] = str(exc)
                state.output.completion = f"ERROR: {exc}"

            return state

        return _run

    return _solver()


# Backward-compat alias
openbot_swt_solver = openbot_test_generation_solver

__all__ = ["openbot_swt_solver", "openbot_test_generation_solver"]
