"""Fix eval solver — thin Inspect AI adapter calling the openbot.evaluation facade.

This solver replaces the old deepagents-based swe_fix.py. It:
  1. Obtains a sandbox from the OpenBot product sandbox factory.
  2. Calls ``openbot.evaluation.run_fix_sample`` which exercises the real
     production fix workflow.
  3. Emits a ``SweBenchPrediction`` to ``state.metadata['prediction']``.

If no sandbox backend is configured (``DAYTONA_API_KEY`` not set), it emits
an empty prediction with a clear error message.
"""

from __future__ import annotations

import logging

from evals.runtime.predictions import SweBenchPrediction, empty_swe_prediction
from openbot.application.sandbox_factory_deps import build_sandbox_factory
from openbot.core.settings import Settings
from openbot.evaluation import run_fix_sample

logger = logging.getLogger(__name__)


def openbot_fix_solver(*, model: str | None = None):
    """Inspect ``@solver`` driving the openbot fix responder on an SWE-bench sample."""
    try:
        from inspect_ai.solver import solver
    except ImportError:

        def solver(fn):  # type: ignore[misc]
            return fn

    @solver
    def _solver():
        async def _run(state, _generate):
            md = state.metadata or {}
            instance_id = str(state.sample_id) if state.sample_id is not None else "anon"
            repo = str(md.get("repo", ""))
            base_commit = str(md.get("base_commit", ""))
            issue_body = state.input_text or ""

            if not repo or not base_commit:
                state.metadata["prediction"] = empty_swe_prediction(
                    instance_id=instance_id,
                    model_name_or_path="openbot",
                ).model_dump()
                state.output.completion = (
                    "ERROR: sample missing repo/base_commit metadata; emitted empty prediction."
                )
                return state

            settings = Settings()
            sandbox_factory = build_sandbox_factory(settings)
            if sandbox_factory is None:
                state.metadata["prediction"] = empty_swe_prediction(
                    instance_id=instance_id,
                    model_name_or_path="openbot",
                ).model_dump()
                state.output.completion = (
                    "ERROR: no sandbox configured (DAYTONA_API_KEY not set); "
                    "emitted empty prediction."
                )
                return state

            async with sandbox_factory() as sandbox:
                outcome = await run_fix_sample(
                    repo=repo,
                    issue_number=0,
                    issue_title=f"SWE-bench fix: {instance_id}",
                    issue_body=issue_body,
                    base_sha=base_commit,
                    clone_url=f"https://github.com/{repo}.git",
                    sandbox=sandbox,
                    run_id=instance_id,
                )

            patch = ""
            if outcome.attempt is not None:
                patch = outcome.attempt.diff or ""

            prediction = SweBenchPrediction(
                instance_id=instance_id,
                model_name_or_path="openbot",
                model_patch=patch,
            )
            state.metadata["prediction"] = prediction.model_dump()
            state.metadata["prediction_json"] = prediction.model_dump_json()
            state.output.completion = prediction.model_dump_json()
            return state

        return _run

    return _solver()


# Compat alias for callers that import by the shorter name.
openbot_swe_solver = openbot_fix_solver

__all__ = ["openbot_fix_solver", "openbot_swe_solver"]
