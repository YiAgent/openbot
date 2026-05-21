"""SWE-QA-Pro Inspect scorer — wraps the development 5-dim judge.

The judge model + prompt + ``judge_answer()`` raw call live in
:mod:`evals.scorers.swe_qa_judge` (official Appendix D prompt text plus
explicit development deviations).
This module is the Inspect AI ``@scorer`` shim that:

1. Pulls the candidate answer off the ``TaskState`` and runs ``judge_answer``.
2. Returns ``value = overall / 50`` so Inspect's mean aggregate stays
   directly comparable across tasks (every task in this repo emits a [0, 1]
   score), with the paper's Table 2 ``Overall`` sum preserved in metadata.
3. Best-effort attaches per-dimension + overall + RL-reward feedback to the
   live LangSmith trace so scores show up in the LangSmith dashboard's
   Feedback tab (and aggregate per-key across runs).

Kept separate from the task file (PRD §6.1) so tasks stay thin — every task
in this repo references a scorer factory, never defines one inline.
"""

from __future__ import annotations

import logging

from inspect_ai.scorer import Score, Target, mean, scorer, stderr
from inspect_ai.solver import TaskState

from evals.inspect.langsmith import ensure_feedback_config
from evals.scorers.swe_qa_judge import (
    JUDGE_DEVIATIONS,
    SWE_QA_JUDGE_MODEL_ID,
    SWE_QA_JUDGE_VERSION,
    judge_answer,
)

logger = logging.getLogger(__name__)


def _post_feedback_to_langsmith(
    *,
    run_id: str,
    scores: dict[str, int],
    overall: int,
    rl_reward: float,
    reference_answer: str | None = None,
    session_id: str | None = None,
    source_info: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    """Attach 5-dim + overall + RL reward as Feedback to a LangSmith trace.

    Per LangSmith docs (https://docs.langchain.com/langsmith/attach-user-feedback),
    ``client.create_feedback(trace_id=..., key=..., score=...)`` is the
    documented way to attach evaluator results to an existing trace. The
    scores appear on the trace's Feedback tab and aggregate per-key on the
    project dashboard.

    Best-effort: any LangSmith failure is logged and swallowed so the eval
    doesn't crash just because tracing is misconfigured.
    """
    try:
        from langsmith import Client

        client = Client()
        feedback_source_info: dict[str, object] = {
            **(source_info or {}),
            "judge_model_id": SWE_QA_JUDGE_MODEL_ID,
            "judge_version": SWE_QA_JUDGE_VERSION,
        }
        dim_config = {"type": "continuous", "min": 1.0, "max": 10.0}
        unit_config = {"type": "continuous", "min": 0.0, "max": 1.0}
        for dim in scores:
            ensure_feedback_config(client, f"swe_qa_pro_{dim}", dim_config)
        ensure_feedback_config(client, "swe_qa_pro_overall_50", unit_config)
        ensure_feedback_config(client, "swe_qa_pro_rl_reward", unit_config)
        # Each dimension as a separate feedback key — lets LangSmith dashboard
        # filter / chart per-dimension trends across runs.
        for dim, val in scores.items():
            client.create_feedback(
                trace_id=run_id,
                session_id=session_id,
                key=f"swe_qa_pro_{dim}",
                score=float(val),
                value=val,
                comment=f"Appendix D rubric, 1-10 (judge v{SWE_QA_JUDGE_VERSION})",
                source_info=feedback_source_info,
                feedback_config={"type": "continuous", "min": 1.0, "max": 10.0},
                extra=extra,
            )
        # Paper Table 2 "Overall" surface — normalized to [0,1] for dashboards.
        client.create_feedback(
            trace_id=run_id,
            session_id=session_id,
            key="swe_qa_pro_overall_50",
            score=overall / 50.0,
            value=overall,
            comment=f"sum of 5 dims (paper Table 2); judge v{SWE_QA_JUDGE_VERSION}",
            correction={"reference_answer": reference_answer} if reference_answer else None,
            source_info=feedback_source_info,
            feedback_config={"type": "continuous", "min": 0.0, "max": 1.0},
            extra=extra,
        )
        client.create_feedback(
            trace_id=run_id,
            session_id=session_id,
            key="swe_qa_pro_rl_reward",
            score=rl_reward,
            comment="paper §3.2 Eq. (4): r = w · s / 10; RL only, not eval metric",
            source_info=feedback_source_info,
            feedback_config={"type": "continuous", "min": 0.0, "max": 1.0},
            extra=extra,
        )
    except Exception as exc:
        # Best-effort observability: tracing should never fail an eval. Logged
        # so misconfigured LangSmith projects are visible instead of silently
        # dropping scores.
        logger.warning(
            "swe_qa_pro: failed to post LangSmith feedback for run %s: %s",
            run_id,
            exc,
        )


@scorer(metrics=[mean(), stderr()], name="swe_qa_pro_judge")
def swe_qa_pro_judge_scorer():  # type: ignore[no-untyped-def]
    """Single-call 5-dim judge scorer for the ``chat_swe_qa_pro`` task.

    Returns ``value = overall / 50`` (normalized [0.1, 1.0]) so the Inspect
    aggregate stays comparable across other tasks. The paper's Table 2
    ``Overall`` (sum of the five 1-10 scores) is preserved in metadata.

    Also posts each dimension + overall + RL reward as
    :func:`langsmith.Client.create_feedback` on the solver's LangSmith trace
    when ``state.metadata['langsmith_run_id']`` is set.
    """

    async def _score(state: TaskState, target: Target) -> Score:
        verdict = judge_answer(
            question=state.input_text,
            reference=target.text,
            candidate=state.output.completion or "",
        )
        scores: dict[str, int] = verdict["scores"]
        overall: int = int(verdict["overall"])
        rl_reward: float = float(verdict["rl_reward"])

        run_id = (state.metadata or {}).get("langsmith_run_id")
        if run_id:
            provider_usage = (state.metadata or {}).get("provider_usage")
            _post_feedback_to_langsmith(
                run_id=str(run_id),
                scores=scores,
                overall=overall,
                rl_reward=rl_reward,
                reference_answer=target.text,
                session_id=str((state.metadata or {}).get("langsmith_project_id") or "") or None,
                source_info={
                    "source": "inspect_scorer",
                    "scorer_name": "swe_qa_pro_judge",
                },
                extra={"provider_usage": dict(provider_usage)}
                if isinstance(provider_usage, dict)
                else None,
            )

        return Score(
            value=overall / 50.0,
            answer=state.output.completion or "",
            explanation=(
                f"correctness={scores['correctness']} "
                f"completeness={scores['completeness']} "
                f"relevance={scores['relevance']} "
                f"clarity={scores['clarity']} "
                f"reasoning={scores['reasoning']} "
                f"overall={overall}/50"
            ),
            metadata={
                **scores,
                "overall_50": overall,  # paper Table 2 column
                "rl_reward": rl_reward,  # paper §3.2 Eq. (4) — RL only
                "judge_model_id": SWE_QA_JUDGE_MODEL_ID,
                "judge_version": SWE_QA_JUDGE_VERSION,
                "judge_deviations": list(JUDGE_DEVIATIONS),
                "langsmith_run_id": run_id,
            },
        )

    return _score
