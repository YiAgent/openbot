"""SWE-QA-Pro Chat eval — PRD §4.1 (``chat_swe_qa_pro`` cell).

Dataset lives in **LangSmith** as ``chat_swe_qa_pro_v1`` (mirrored from HF
``TIGER-Lab/SWE-QA-Pro-Bench`` by
``evals/scripts/build_chat_swe_qa_pro_dataset.py``). The eval pulls Examples
via :func:`evals.common.datasets.langsmith_dataset` — same pattern as
``review_martian``, so all evals load through one uniform code path.

Two task entries — both map to columns in SWE-QA-Pro Table 2:

  - :func:`chat_swe_qa_pro_baseline` — deepagents direct answer (no tools,
    no sandbox). Maps to the paper's "no +Agent" baseline column.
  - :func:`chat_swe_qa_pro_openbot` — paper's "+Agent" variant. Each sample
    spins up a Docker sandbox where the target repo is cloned at the pinned
    commit into ``/testbed`` (via the sample ``setup`` script generated in
    :func:`evals.common.datasets.qa_example_to_agent_sample`). The solver
    drives deepagents with the verbatim Appendix D prompts + the
    sandbox-aware tool suite (``ls`` / ``glob`` / ``grep`` / ``read_file`` /
    ``execute``) from
    :class:`evals.solvers.inspect_sandbox_backend.InspectSandboxBackend`.

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

    # Baseline (closed-book direct answer)
    doppler run --project openbot --config dev -- \\
        uv run inspect eval \\
        'evals/tasks/chat_swe_qa_pro.py@chat_swe_qa_pro_baseline' \\
        --limit 5

    # +Agent (Docker sandbox + paper Appendix D protocol)
    doppler run --project openbot --config dev -- \\
        uv run inspect eval \\
        'evals/tasks/chat_swe_qa_pro.py@chat_swe_qa_pro_openbot' \\
        --limit 5
"""

from __future__ import annotations

from inspect_ai import Task, task
from inspect_ai.scorer import mean, stderr

from evals.common.datasets import (
    langsmith_dataset,
    qa_example_to_agent_sample,
    qa_example_to_sample,
)
from evals.common.langsmith import configure_tracing_for_dataset
from evals.common.langsmith_experiments import LangSmithExperiment
from evals.scorers.swe_qa_judge import SWE_QA_JUDGE_MODEL_ID, SWE_QA_JUDGE_VERSION
from evals.scorers.swe_qa_pro import swe_qa_pro_judge_scorer
from evals.solvers.swe_qa import (
    deepagents_agent_swe_qa_solver,
    deepagents_baseline_swe_qa_solver,
)

_DATASET_VERSION = "chat_swe_qa_pro_v1"
_DATASET_SOURCE = "langsmith:chat_swe_qa_pro_v1 (mirror of huggingface:TIGER-Lab/SWE-QA-Pro-Bench)"


@task
def chat_swe_qa_pro_baseline() -> Task:
    """Deepagents direct-answer baseline + development 5-dim judge.

    Prompt text follows Appendix D, but this local dev scorer intentionally
    differs from the paper protocol by using the Anthropic judge and a single
    call instead of GPT-5 plus 3-run averaging. For the paper's "+Agent"
    column use :func:`chat_swe_qa_pro_openbot` instead.
    """
    configure_tracing_for_dataset(_DATASET_VERSION)
    # Surface this run as a LangSmith Experiment. We use a dedicated
    # ``swe_qa_pro_judge`` feedback key on the Experiment Run for the
    # normalized 0-1 scalar so it shows up as a metric column on the
    # Experiments tab — distinct from the 5-dim ``swe_qa_pro_*`` feedback
    # the scorer writes on the underlying *trace* (those remain the source
    # of truth for Table 2 comparisons; different LangSmith object, no
    # collision). ``instance_id_field="id"`` maps ``state.sample_id`` to
    # the LangSmith Example via ``inputs.id`` (see
    # evals/scripts/build_chat_swe_qa_pro_dataset.py).
    experiment = LangSmithExperiment.start(
        dataset_name=_DATASET_VERSION,
        solver_family="deepagents_baseline",
        instance_id_field="id",
    )
    return Task(
        dataset=langsmith_dataset(_DATASET_VERSION, converter=qa_example_to_sample),
        solver=deepagents_baseline_swe_qa_solver(),
        scorer=experiment.wrap(
            swe_qa_pro_judge_scorer(),
            metrics=[mean(), stderr()],
            scorer_name="swe_qa_pro_judge",
            feedback_key="swe_qa_pro_judge",
            feedback_config={"type": "continuous", "min": 0.0, "max": 1.0},
        ),
        metadata={
            "dataset_version": _DATASET_VERSION,
            "dataset_source": _DATASET_SOURCE,
            "solver_id": "deepagents_baseline",
            "solver_family": "deepagents_baseline",
            "judge_label": "swe_qa_pro_5dim",
            "judge_model_id": SWE_QA_JUDGE_MODEL_ID,
            "judge_prompt_version": SWE_QA_JUDGE_VERSION,
            **experiment.metadata(),
        },
    )


@task
def chat_swe_qa_pro_openbot() -> Task:
    """SWE-QA-Pro paper +Agent variant — deepagents inside a Docker sandbox.

    Maps to the paper's "+Agent" Table 2 column. Each sample:

      1. The solver spins up its own Docker container via
         :class:`evals.sandboxes.DockerSandboxBackend.create_for_sample`,
         which clones the repo at the pinned commit into ``/workspace``.
      2. ``deepagents_agent_swe_qa_solver`` drives deepagents with the
         verbatim Appendix D system + user prompts and the sandbox-aware
         ``ls`` / ``glob`` / ``grep`` / ``read_file`` / ``execute`` tools.
      3. The final answer is extracted from the required
         ``<finish>...</finish>`` block before it reaches the judge, and
         packaged into a :class:`SweQaProAnswer` for downstream analysis.

    No Inspect-side Docker sandbox is involved — agent execution is fully
    decoupled from the evaluation surface. The scorer is the same 5-dim
    dev judge as the baseline so the two tasks remain directly comparable
    in LangSmith Experiments (baseline vs +Agent uplift on the same
    dataset).
    """
    configure_tracing_for_dataset(_DATASET_VERSION)
    experiment = LangSmithExperiment.start(
        dataset_name=_DATASET_VERSION,
        solver_family="deepagents_agent",
        instance_id_field="id",
    )
    return Task(
        dataset=langsmith_dataset(_DATASET_VERSION, converter=qa_example_to_agent_sample),
        solver=deepagents_agent_swe_qa_solver(),
        scorer=experiment.wrap(
            swe_qa_pro_judge_scorer(),
            metrics=[mean(), stderr()],
            scorer_name="swe_qa_pro_judge",
            feedback_key="swe_qa_pro_judge",
            feedback_config={"type": "continuous", "min": 0.0, "max": 1.0},
        ),
        metadata={
            "dataset_version": _DATASET_VERSION,
            "dataset_source": _DATASET_SOURCE,
            "solver_id": "deepagents_agent",
            "solver_family": "deepagents_agent",
            "judge_label": "swe_qa_pro_5dim",
            "judge_model_id": SWE_QA_JUDGE_MODEL_ID,
            "judge_prompt_version": SWE_QA_JUDGE_VERSION,
            **experiment.metadata(),
        },
    )
