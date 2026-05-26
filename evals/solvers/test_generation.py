"""SWT-bench test-generation solver — calls the openbot reproduce agent.

The reproduce agent writes a pytest test file that triggers the reported
failure, runs it to confirm it fails, then emits the git unified diff via
``git_diff()``.  This solver maps that diff to ``SwtBenchPrediction.model_patch``
so the upstream Docker harness can grade it offline.

Architecture mirrors the other three solvers (review / fix / chat):
  1. Build a sandbox factory from ``openbot.application.sandbox_factory_deps``
     (same call the fix solver makes — never injected via sample metadata).
  2. Delegate the full lifecycle to ``run_test_generation_sample`` — same
     pattern as ``run_fix_sample`` (clone → agent → close).
  3. Store a ``SwtBenchPrediction`` dict in ``state.metadata["prediction"]``
     for the ``prediction_exporter`` scorer.

Data shape (from ``issue_row_to_sample``):
  state.sample_id  → instance_id  (e.g. "astropy__astropy-12907")
  state.input_text → problem_statement  (HuggingFace "problem_statement" field)
  state.metadata   → {"repo": ..., "base_commit": ..., "version": ...}

SWT-bench rows have no real GitHub issue numbers; ``issue_number=0`` is the
stable placeholder (same convention as the fix solver).
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
            from openbot.application.sandbox_factory_deps import build_sandbox_factory
            from openbot.core.settings import Settings

            instance_id = str(state.sample_id) if state.sample_id is not None else "anon"
            metadata = state.metadata or {}

            repo = str(metadata.get("repo", ""))
            base_sha = str(metadata.get("base_commit", ""))
            # problem_statement lives in state.input_text (set by issue_row_to_sample).
            problem_statement = state.input_text or ""
            clone_url = f"https://github.com/{repo}.git" if repo else ""

            if not repo or not base_sha:
                logger.warning(
                    "swt_bench_solver_missing_metadata instance_id=%s — "
                    "emitting empty prediction (repo=%r base_sha=%r)",
                    instance_id,
                    repo,
                    base_sha,
                )
                prediction = empty_swt_prediction(
                    instance_id=instance_id,
                    model_name_or_path="openbot",
                )
                state.metadata["prediction"] = prediction.model_dump()
                state.metadata["prediction_json"] = prediction.model_dump_json()
                state.output.completion = f"NO_METADATA: {instance_id}"
                return state

            # Build sandbox factory the same way the fix solver does — never
            # injected via sample metadata.  Public SWT-bench repos need no token.
            sandbox_factory = build_sandbox_factory(Settings())
            if sandbox_factory is None:
                logger.warning(
                    "swt_bench_solver_no_sandbox instance_id=%s — "
                    "emitting empty prediction (DAYTONA_API_KEY not set?)",
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
                    # SWT-bench rows have no real GitHub issue numbers.
                    issue_number=0,
                    problem_statement=problem_statement,
                    base_sha=base_sha,
                    clone_url=clone_url,
                    sandbox_factory=sandbox_factory,
                    clone_token="",
                    run_id=instance_id,
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
