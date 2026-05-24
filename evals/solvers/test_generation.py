"""Test generation eval solver — explicit unsupported capability stub.

This solver replaces the old deepagents-based swe_test.py. Until OpenBot
product code has a real test-generation capability, this solver emits an
empty ``SwtBenchPrediction`` with ``unsupported=true`` metadata so:

  1. LangSmith experiment rows remain stable (no missing rows).
  2. It is impossible to confuse the result with a real product attempt.
  3. Inspect scores show as 0 / value=0 (export failed) rather than erroring.

When test generation is implemented, replace this file's body with a call
to ``openbot.evaluation.run_test_generation_sample``.
"""

from __future__ import annotations

import logging

from evals.runtime.predictions import empty_swt_prediction

logger = logging.getLogger(__name__)

_UNSUPPORTED_REASON = "Test generation is not yet implemented in v0.1."


def openbot_test_generation_solver(*, model: str | None = None):
    """Inspect ``@solver`` — returns an explicit unsupported-capability prediction."""
    try:
        from inspect_ai.solver import solver
    except ImportError:

        def solver(fn):
            return fn

    @solver
    def _solver():
        async def _run(state, _generate):
            instance_id = str(state.sample_id) if state.sample_id is not None else "anon"

            prediction = empty_swt_prediction(
                instance_id=instance_id,
                model_name_or_path="openbot",
            )
            state.metadata["prediction"] = prediction.model_dump()
            state.metadata["prediction_json"] = prediction.model_dump_json()
            state.metadata["unsupported"] = True
            state.metadata["unsupported_reason"] = "not_implemented"
            state.output.completion = (
                f"UNSUPPORTED: {_UNSUPPORTED_REASON} instance_id={instance_id}"
            )
            return state

        return _run

    return _solver()


# Backward-compat alias
openbot_swt_solver = openbot_test_generation_solver

__all__ = ["openbot_swt_solver", "openbot_test_generation_solver"]
