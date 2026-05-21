"""SWE-QA-Pro solver — deepagents +Agent variant (Docker sandbox).

Each sample runs in its own sandbox with the repo at the pinned commit.
Final answer is schema-bound to SweQaProAnswer via the structured finalizer.
"""

from __future__ import annotations

from typing import Any

from evals.agents.baseline import build_run_config, resolve_model
from evals.agents.chat import build_chat_agent, build_chat_user_message
from evals.common.predictions import SweQaProAnswer
from evals.common.termination import assert_clean_termination
from evals.common.usage import aggregate_provider_usage
from evals.sandboxes import RepoSpec, create_sandbox_for_sample

_AGENT_REPO_PATH = "/workspace"


async def _invoke_agent_with_modal(
    *,
    question: str,
    repo: str,
    commit_id: str,
    repo_path: str,
    model: str,
    ls_config: Any,
) -> dict[str, Any]:
    """Run the +Agent flow: spin up the sandbox, drive deepagents, return the
    structured answer.
    """
    # build_chat_agent wraps with the structured finalizer (middleware.py).
    from langsmith.run_helpers import get_current_run_tree

    backend = await create_sandbox_for_sample(
        repo_spec=RepoSpec(repo=repo, base_commit=commit_id, workspace=repo_path),
    )
    try:
        agent = build_chat_agent(model=model, backend=backend)
        user_msg = build_chat_user_message(question=question, repo_path=repo_path)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_msg}]},
            config=ls_config,
        )

        assert_clean_termination(
            result,
            requires_structured_response=True,
            structured_response_type=SweQaProAnswer,
        )
        structured_raw = result["structured_response"]
        if isinstance(structured_raw, SweQaProAnswer):
            structured = structured_raw
        else:
            # langchain provider strategies sometimes leave us a dict.
            structured = SweQaProAnswer.model_validate(structured_raw)

        messages = list(result.get("messages") or [])
        usage = aggregate_provider_usage(messages)
        run = get_current_run_tree()
        return {
            "text": structured.answer,
            "structured": structured.model_dump(),
            "usage": usage,
            "langsmith_run_id": str(run.id) if run is not None else None,
            "modal_sandbox_id": backend.id,
        }
    finally:
        await backend.aclose()


def deepagents_baseline_swe_qa_solver(*, model: str | None = None):  # type: ignore[no-untyped-def]
    """Inspect ``@solver`` — paper +Agent variant on Modal.

    Each sample runs in its own Modal sandbox with the repo cloned at the
    pinned commit into ``/workspace``. Uses the verbatim Appendix D system
    + user prompts and the deepagents sandbox-aware tool suite via
    :class:`DockerSandboxBackend`. The final answer is extracted from the
    required ``<finish>...</finish>`` block and packaged into a
    :class:`SweQaProAnswer` (also stashed in ``state.metadata['prediction']``
    so downstream analysis sees a uniform schema across eval cells).
    """
    from inspect_ai.solver import Generate, Solver, TaskState, solver
    from langsmith import traceable

    resolved_model = resolve_model(override=model)

    @solver
    def _solver() -> Solver:
        async def _run(state: TaskState, _generate: Generate) -> TaskState:
            md = state.metadata or {}
            sample_id = str(state.sample_id) if state.sample_id is not None else ""
            repo = str(md.get("repo", ""))
            commit_id = str(md.get("commit_id", ""))
            repo_path = str(md.get("repo_path", _AGENT_REPO_PATH))
            dataset_version = str(md.get("dataset_version", "chat_swe_qa_pro"))

            ls_config = build_run_config(
                sample_id=sample_id or "anon",
                dataset_version=dataset_version,
                solver_family=str(md.get("solver_family", "deepagents_baseline")),
                model=resolved_model,
                git_sha=md.get("git_sha"),
                extra_metadata={
                    "repo": repo,
                    "commit_id": commit_id,
                    "repo_path": repo_path,
                    "qa_class": str(md.get("qa_class", "")),
                    "qa_subclass": str(md.get("qa_subclass", "")),
                    "cluster_id": str(md.get("cluster_id", "")),
                    "dataset_sha256": str(md.get("dataset_sha256", "")),
                    "langsmith_experiment_name": md.get("langsmith_experiment_name"),
                },
            )

            traced = traceable(
                name="chat_swe_qa_pro_openbot.sample",
                run_type="chain",
                tags=["chat_swe_qa_pro", "deepagents_baseline", "appendix_d", "modal"],
                metadata={
                    "sample_id": sample_id,
                    "repo": repo,
                    "commit_id": commit_id,
                    "repo_path": repo_path,
                    "qa_class": str(md.get("qa_class", "")),
                    "qa_subclass": str(md.get("qa_subclass", "")),
                    "cluster_id": str(md.get("cluster_id", "")),
                    "model_id": resolved_model,
                    "dataset_version": dataset_version,
                    "dataset_sha256": str(md.get("dataset_sha256", "")),
                },
            )(_invoke_agent_with_modal)

            out = await traced(
                question=state.input_text,
                repo=repo,
                commit_id=commit_id,
                repo_path=repo_path,
                model=resolved_model,
                ls_config=ls_config,
            )

            if out["usage"] is not None:
                state.metadata["provider_usage"] = out["usage"]
            if out["langsmith_run_id"]:
                state.metadata["langsmith_run_id"] = out["langsmith_run_id"]
            if out.get("modal_sandbox_id"):
                state.metadata["modal_sandbox_id"] = out["modal_sandbox_id"]
            state.metadata["prediction"] = out["structured"]
            state.output.completion = out["text"]
            return state

        return _run

    return _solver()
