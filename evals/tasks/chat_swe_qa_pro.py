"""SWE-QA-Pro Chat eval — PRD §4.1 (``chat_swe_qa_pro`` cell).

Dataset lives in **LangSmith** as ``chat_swe_qa_pro_v1`` (mirrored from HF
``TIGER-Lab/SWE-QA-Pro-Bench`` by
``evals/scripts/build_chat_swe_qa_pro_dataset.py``). The eval pulls Examples
via :func:`evals.common.datasets.langsmith_dataset` — same pattern as
``review_martian``, so all evals load through one uniform code path.

Single task entry maps to the paper's Table 2 ``+Agent`` column:

  - :func:`chat_swe_qa_pro_openbot` — each sample spins up a Docker
    sandbox where the target repo is cloned at the pinned commit into
    ``/workspace``. The solver drives deepagents with the verbatim
    Appendix D prompts + the sandbox-aware tool suite (``ls`` / ``glob``
    / ``grep`` / ``read_file`` / ``execute``) from
    :class:`evals.sandboxes.DockerSandboxBackend`.

The closed-book ("Direct" column) baseline was removed — we only run the
agent variant because that's the configuration users actually evaluate
against. Re-introduce a baseline later only if a head-to-head Direct vs
+Agent comparison is needed.

Scoring lives in :mod:`evals.scorers.swe_qa_pro` (Inspect ``@scorer``
factory) which delegates to the development judge in
:mod:`evals.scorers.swe_qa_judge`: it uses the official Appendix D prompt
text, but intentionally keeps the Anthropic dev model plus single-call
scoring deviation recorded in ``judge_deviations``. ``Overall = sum`` still
matches Table 2. Each sample's root LangSmith span gets per-dim Feedback
attached so scores show in the project dashboard.

Run::

    # First time: publish the LangSmith dataset
    doppler run --project openbot --config dev -- \\
        uv run python -m evals.scripts.build_chat_swe_qa_pro_dataset

    # +Agent (Docker sandbox + paper Appendix D protocol)
    doppler run --project openbot --config dev -- \\
        uv run inspect eval \\
        'evals/tasks/chat_swe_qa_pro.py@chat_swe_qa_pro_openbot' \\
        --limit 5
"""

from __future__ import annotations

from inspect_ai import Task, task
from inspect_ai.scorer import mean, stderr

from evals.common.config import get_eval_config
from evals.common.datasets import langsmith_dataset, qa_example_to_agent_sample
from evals.inspect.langsmith import LangSmithExperiment, configure_tracing_for_dataset
from evals.scorers.swe_qa_judge import SWE_QA_JUDGE_MODEL_ID, SWE_QA_JUDGE_VERSION
from evals.scorers.swe_qa_pro import swe_qa_pro_judge_scorer
from evals.solvers.swe_qa import deepagents_baseline_swe_qa_solver


@task
def chat_swe_qa_pro_openbot() -> Task:
    """SWE-QA-Pro paper +Agent variant — deepagents inside a Docker sandbox.

    Maps to the paper's "+Agent" Table 2 column. Each sample:

      1. The solver spins up its own Docker container via
         :class:`evals.sandboxes.DockerSandboxBackend.create_for_sample`,
         which clones the repo at the pinned commit into ``/workspace``.
      2. ``deepagents_baseline_swe_qa_solver`` drives deepagents with the
         verbatim Appendix D system + user prompts and the sandbox-aware
         ``ls`` / ``glob`` / ``grep`` / ``read_file`` / ``execute`` tools.
      3. The final answer is extracted from the required
         ``<finish>...</finish>`` block before it reaches the judge, and
         packaged into a :class:`SweQaProAnswer` for downstream analysis.

    No Inspect-side Docker sandbox is involved — agent execution is fully
    decoupled from the evaluation surface.
    """
    catalog = get_eval_config().catalog
    dataset_version = catalog.chat.dataset_version
    configure_tracing_for_dataset(dataset_version)
    # Surface this run as a LangSmith Experiment. The ``swe_qa_pro_judge``
    # feedback key on the Experiment Run carries the normalized 0-1 scalar
    # so it shows as a metric column on the Experiments tab — distinct
    # from the 5-dim ``swe_qa_pro_*`` feedback the scorer writes on the
    # underlying *trace* (those remain Table 2 source of truth; different
    # LangSmith object, no collision). ``instance_id_field="id"`` maps
    # ``state.sample_id`` to the LangSmith Example via ``inputs.id`` (see
    # evals/scripts/build_chat_swe_qa_pro_dataset.py).
    experiment = LangSmithExperiment.start(
        dataset_name=dataset_version,
        solver_family=catalog.solver_family_baseline,
        instance_id_field="id",
    )
    return Task(
        dataset=langsmith_dataset(dataset_version, converter=qa_example_to_agent_sample),
        solver=deepagents_baseline_swe_qa_solver(),
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
            "solver_id": catalog.solver_family_baseline,
            "solver_family": catalog.solver_family_baseline,
            "judge_label": "swe_qa_pro_5dim",
            "judge_model_id": SWE_QA_JUDGE_MODEL_ID,
            "judge_prompt_version": SWE_QA_JUDGE_VERSION,
            **experiment.metadata(),
        },
    )
