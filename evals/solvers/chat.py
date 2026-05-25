"""Chat eval solver — thin Inspect AI adapter calling the openbot.evaluation facade.

This solver replaces the old deepagents-based swe_qa.py. It calls
``openbot.evaluation.run_chat_sample`` which exercises the real production
chat responder.
"""

from __future__ import annotations

import logging

from openbot.evaluation import run_chat_sample

logger = logging.getLogger(__name__)


def openbot_chat_solver(*, model: str | None = None):
    """Inspect ``@solver`` driving the openbot chat responder on a SWE-QA-Pro sample."""
    try:
        from inspect_ai.solver import solver
    except ImportError:

        def solver(fn):  # type: ignore[misc]
            return fn

    @solver
    def _solver():
        async def _run(state, _generate):
            md = state.metadata or {}
            sample_id = str(state.sample_id) if state.sample_id is not None else "anon"
            repo = str(md.get("repo", ""))
            question = state.input_text or ""

            answer = await run_chat_sample(
                repo=repo,
                user_request=question,
                run_id=sample_id,
            )

            state.output.completion = answer
            return state

        return _run

    return _solver()


# Backward-compat alias
openbot_swe_qa_solver = openbot_chat_solver

__all__ = ["openbot_chat_solver", "openbot_swe_qa_solver"]
