"""SWE-QA-Pro Chat eval — PRD §4.1 (``chat_swe_qa`` eval surface).

Dataset lives in **LangSmith** as ``chat_swe_qa_pro_v1``. The eval pulls
Examples via :func:`evals.data._samples.langsmith_dataset`.

Trace routing to the eval project (``LANGSMITH_PROJECT_EVAL``) happens at
``evals.runtime`` package import. LangChain auto-uploads per-sample traces,
and the ``langsmith_hook.py`` anchors each sample to a LangSmith Experiment
Run automatically from ``Task.metadata``.

Run::

    # First time: publish the LangSmith dataset
    doppler run --project openbot --config dev -- \\
        uv run python -m evals.data refresh chat

    doppler run --project openbot --config dev -- \\
        uv run inspect eval 'evals/tasks/chat_swe_qa.py@chat_swe_qa' --limit 5
"""

from __future__ import annotations

from inspect_ai import Task, task

from evals.data import CHAT
from evals.scorers.swe_qa_pro import swe_qa_pro_judge_scorer
from evals.solvers.chat import openbot_chat_solver


@task
def chat_swe_qa() -> Task:
    """SWE-QA-Pro eval with the OpenBot chat solver.

    Each sample calls the production OpenBot chat responder and scores
    the response with the 5-dimensional SWE-QA-Pro judge.
    """
    return CHAT.build_task(
        solver=openbot_chat_solver(),
        scorer=swe_qa_pro_judge_scorer(),
    )
