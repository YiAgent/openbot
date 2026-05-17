"""SWE-QA-Pro solver — paper +Agent variant (Docker sandbox + read tools).

SWE-QA-Pro (TIGER-Lab, arXiv 2603.16124) evaluates *repository-level* code
understanding. We only run the paper's Table 2 "+Agent" column:

- :func:`deepagents_agent_swe_qa_solver` — each sample runs in its own
  **Docker sandbox** with the target repo cloned at the pinned commit
  into ``/workspace``. The agent uses deepagents' native sandbox-aware
  tools (``ls`` / ``glob`` / ``grep`` / ``read_file`` / ``execute``) via
  :class:`DockerSandboxBackend`. Final answer is schema-bound to
  :class:`SweQaProAnswer` so the judge always sees a well-formed body +
  structured citations regardless of how the agent formats its prose.

The closed-book "Direct" baseline was removed — see ``evals/tasks/
chat_swe_qa_pro.py`` module docstring for rationale. The agent sandbox
here is fully separate from any Inspect-managed sandbox: agent execution
is decoupled from the evaluation surface.
"""

from __future__ import annotations

from typing import Any

from evals.common.deepagents_baseline import (
    build_baseline_agent,
    build_run_config,
    resolve_model,
)
from evals.common.predictions import SweQaProAnswer
from evals.common.termination import assert_clean_termination
from evals.common.usage import aggregate_provider_usage
from evals.sandboxes import RepoSpec, create_sandbox_for_sample

# ─── Verbatim Appendix D — "Prompt Template for Generating Answer" ─────────
# Source: arXiv 2603.16124v1 Appendix D ("Model: All Evaluated Model").
# Kept byte-faithful so the +Agent column stays reproducible vs the paper.

SWE_QA_PRO_PAPER_AGENT_SYSTEM: str = """You are a codebase analysis agent operating in a strictly read-only environment.
Your task is to answer SWE-related questions by analyzing source code, configuration, documentation, and tests.
You must prioritize correctness, completeness, clarity, relevance and evidence-based reasoning when answering given questions within 25 max turns.
PROCESS PROTOCOL (MANDATORY)
For every question, you MUST follow this process:
1. Planning
Before calling any tools, you MUST output a short planning explanation at each turn.
* Explain step by step what you have found so far from the current context, and what you will inspect next and why.
* This reasoning MUST be explicit and visible.
2. Investigation
* Call one or more read-only tools to gather evidence.
* Multiple tool calls in one turn are allowed.
3. Synthesis
* Combine evidence acoss multiple files or components.
* Do NOT rely on a single file unless clearly justified.
4. Finalization
* Produce a final answer following the OUTPUT PROTOCOL.
TOOL USAGE RULES
Available tools:
* semantic_search: find relevant files, symbols, or modules.
* viewcodebase: inspect structure or specific file sections.
* Prefer 'concise=True' first; use 'viewrange' when needed. Prefer using viewcodebase; avoid using ls -l or ls -R whenever possible. Don't use tree without -L.
* executereadonlycommand: small, focused inspection tasks that require raw command output (Avoid using command-line operations that produce excessive and uncontrollable output, DON't use ls -R path and ls -lR path as a command).
You may call one or more functions to assist with the user query.
You are provided with function signatures within <tools></tools> XML tags:
<tools>
{tools}
</tools>
OUTPUT PROTOCOL (STRICT)
You MUST follow this output structure at each assistant turn:
1. Reasoning
* Before any tool call, output only your step by step planning explanation
2. Final Answer
* When ready to finish, do NOT call any more tools — instead emit your
  final answer using the structured response binding. The harness will
  ask you to fill the ``SweQaProAnswer`` schema with two fields:
    - ``answer``: the natural-language answer body. NO copied code blocks.
    - ``citations``: list of evidence objects, each
        ``{relative_path: str, line_start: int, line_end: int}``,
      using paths relative to ``repo_path`` (NOT absolute). Inclusive
      1-indexed line numbers.
Any non-structured final reply violates this protocol.
The working directory (where the code is executed) is /data/songcheng/SWE-QA-Pro-dev/eval. Now the code repo at repopath. Please use absolute paths in all tools."""

SWE_QA_PRO_PAPER_AGENT_USER_TEMPLATE: str = """Repository Path: {repo_path}
Question:{question}
Instructions:
- Please analyze the codebase to answer this question.
- Provide a step-by-step explanation before calling any tools.
- Follow this workflow:
1) Inspect the repository structure
2) Search for relevant files and symbols
3) Examine specific implementations
4) Cross-validate your findings
5) When done investigating, emit your final answer via the structured
   ``SweQaProAnswer`` response (answer + citations), not as free text."""

# ─── End verbatim block ───────────────────────────────────────────────────


# Usage aggregation lives in evals.common.usage — sums across all AI
# messages, matching LangSmith's trace-side aggregation.


# ─── +Agent: Modal sandbox with repo cloned at commit ──────────────────────
#
# Removed the closed-book ("Direct" column in SWE-QA-Pro Table 2) baseline
# solver — we only run the +Agent variant. The agent inspects the actual
# repo at the pinned commit via DockerSandboxBackend's read-only tools.


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

    The agent is built with ``response_format=SweQaProAnswer``. The
    baseline wraps the compiled graph with
    :func:`~evals.common.structured_finalizer.wrap_agent_with_finalizer`,
    so structured output is enforced by a dedicated post-loop LLM call
    rather than by binding ``tool_choice=forced`` into the agent loop
    (which conflicts with Anthropic's extended-thinking and is silently
    dropped to optional). The wrapper populates
    ``result["structured_response"]`` with a validated
    :class:`SweQaProAnswer` whenever the agent produced *any* prose;
    transport failure or an empty trace still raises
    :class:`~evals.common.termination.AgentTerminationError` so Inspect
    treats the sample as errored under ``--retry-on-error`` /
    ``--fail-on-error``.
    """
    from langsmith.run_helpers import get_current_run_tree

    backend = await create_sandbox_for_sample(
        repo_spec=RepoSpec(repo=repo, base_commit=commit_id, workspace=repo_path),
    )
    try:
        agent = build_baseline_agent(
            system_prompt=SWE_QA_PRO_PAPER_AGENT_SYSTEM,
            model=model,
            backend=backend,
            response_format=SweQaProAnswer,
        )
        user_msg = SWE_QA_PRO_PAPER_AGENT_USER_TEMPLATE.format(
            repo_path=repo_path,
            question=question,
        )
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


def deepagents_agent_swe_qa_solver(*, model: str | None = None):  # type: ignore[no-untyped-def]
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
                solver_family=str(md.get("solver_family", "deepagents_agent")),
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
                tags=["chat_swe_qa_pro", "deepagents_agent", "appendix_d", "modal"],
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
