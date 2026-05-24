"""SWE-QA-Pro Chat eval — PRD §4.1 (``chat_swe_qa`` eval surface).

Dataset lives in **LangSmith** as ``chat_swe_qa_pro_v1``. The eval pulls
Examples via :func:`evals.runtime.datasets.langsmith_dataset`.

Run::

    # First time: publish the LangSmith dataset
    doppler run --project openbot --config dev -- \\
        uv run python -m evals.scripts.build_chat_swe_qa_pro_dataset

    doppler run --project openbot --config dev -- \\
        uv run inspect eval 'evals/tasks/chat_swe_qa.py@chat_swe_qa' --limit 5
"""

from __future__ import annotations

from inspect_ai import Task, task
from inspect_ai.scorer import mean, stderr

from evals.runtime.config import get_eval_config
from evals.runtime.datasets import langsmith_dataset, qa_example_to_agent_sample
from evals.runtime.langsmith import LangSmithExperiment, configure_tracing_for_dataset
from evals.scorers.swe_qa_judge import SWE_QA_JUDGE_MODEL_ID, SWE_QA_JUDGE_VERSION
from evals.scorers.swe_qa_pro import swe_qa_pro_judge_scorer
from evals.solvers.chat import openbot_chat_solver


@task
def chat_swe_qa() -> Task:
    """SWE-QA-Pro eval with the OpenBot chat solver.

    Each sample calls the production OpenBot chat responder and scores
    the response with the 5-dimensional SWE-QA-Pro judge.
    """
    catalog = get_eval_config().catalog
    dataset_version = catalog.chat.dataset_version
    configure_tracing_for_dataset(dataset_version)
    experiment = LangSmithExperiment.start(
        dataset_name=dataset_version,
        solver_family=catalog.solver_family_baseline,
        instance_id_field="id",
    )
    return Task(
        dataset=langsmith_dataset(dataset_version, converter=qa_example_to_agent_sample),
        solver=openbot_chat_solver(),
        scorer=experiment.wrap(
            swe_qa_pro_judge_scorer(),
            metrics=[mean(), stderr()],
            scorer_name="swe_qa_pro_judge",
            feedback_key="swe_qa_pro_judge",
            feedback_config=catalog.unit_feedback_config,
        ),
        metadata={
            "dataset_version": dataset_version,
            "dataset_source": catalog.chat.dataset_source,
            "solver_id": "chat",
            "solver_family": catalog.solver_family_baseline,
            "judge_label": "swe_qa_pro_5dim",
            "judge_model_id": SWE_QA_JUDGE_MODEL_ID,
            "judge_prompt_version": SWE_QA_JUDGE_VERSION,
            **experiment.metadata(),
        },
    )
