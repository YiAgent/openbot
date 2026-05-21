"""SWT-Bench Verified solver — deepagents inside an isolated sandbox.

Writes regression tests only (no production-code edits). Captures
git diff into SwtBenchPrediction; grading is offline via SWT-Bench harness.
"""

from __future__ import annotations

import logging

from inspect_ai.solver import Generate, Solver, TaskState, solver

from evals.agents.baseline import build_run_config, resolve_model
from evals.agents.test_generation import (
    build_test_generation_agent,
    build_test_generation_user_message,
)
from evals.common.messages import join_message_texts
from evals.common.predictions import SwtBenchPrediction, empty_swt_prediction
from evals.common.termination import assert_clean_termination
from evals.common.usage import aggregate_provider_usage
from evals.sandboxes import RepoSpec, create_sandbox_for_sample

logger = logging.getLogger(__name__)


def deepagents_baseline_swt_solver(*, model: str | None = None) -> Solver:
    """Inspect ``@solver`` driving deepagents on an SWT-Bench sample.

    Builds one fresh Modal sandbox per sample, runs the agent against the
    issue, captures the diff into a :class:`SwtBenchPrediction`, and
    tears the sandbox down. Scoring is downstream via the official
    SWT-Bench Docker harness.
    """
    resolved_model = resolve_model(
        override=model,
    )

    @solver
    def _solver() -> Solver:
        async def _run(state: TaskState, _generate: Generate) -> TaskState:
            md = state.metadata or {}
            instance_id = str(state.sample_id) if state.sample_id is not None else "anon"
            repo = str(md.get("repo", ""))
            base_commit = str(md.get("base_commit", ""))
            if not repo or not base_commit:
                state.metadata["prediction"] = empty_swt_prediction(
                    instance_id=instance_id,
                    model_name_or_path=resolved_model,
                ).model_dump()
                state.output.completion = (
                    "ERROR: sample missing repo/base_commit metadata; emitted empty prediction."
                )
                return state

            backend = await create_sandbox_for_sample(
                repo_spec=RepoSpec(repo=repo, base_commit=base_commit),
            )
            try:
                agent = build_test_generation_agent(model=resolved_model, backend=backend)
                user_msg = build_test_generation_user_message(state.input_text)
                ls_config = build_run_config(
                    sample_id=instance_id,
                    dataset_version=md.get("dataset_version", "test_swt_bench_verified"),
                    solver_family=md.get("solver_family", "deepagents_baseline"),
                    model=resolved_model,
                    git_sha=md.get("git_sha"),
                    extra_metadata={
                        "repo": repo,
                        "base_commit": base_commit,
                        "modal_sandbox_id": backend.id,
                        "langsmith_experiment_name": md.get("langsmith_experiment_name"),
                    },
                )
                result = await agent.ainvoke(
                    {"messages": [{"role": "user", "content": user_msg}]},
                    config=ls_config,
                )

                # Raises AgentTerminationError on incomplete runs.
                assert_clean_termination(result, requires_structured_response=False)
                patch = await backend.acapture_diff()
                prediction = SwtBenchPrediction(
                    instance_id=instance_id,
                    model_name_or_path=resolved_model,
                    model_patch=patch,
                )
                state.metadata["prediction"] = prediction.model_dump()
                state.metadata["prediction_json"] = prediction.model_dump_json()
                state.metadata["modal_sandbox_id"] = backend.id

                messages = list(result.get("messages", []))
                provider_usage = aggregate_provider_usage(messages)
                if provider_usage is not None:
                    state.metadata["provider_usage"] = provider_usage
                state.metadata["agent_raw_output"] = join_message_texts(messages)
                state.output.completion = prediction.model_dump_json()
            finally:
                await backend.aclose()
            return state

        return _run

    return _solver()
