"""SWE-bench Verified solver — deepagents inside an isolated Modal sandbox.

Architecture (PRD §3.1, post-refactor):

- The agent runs in a **Modal sandbox** with the target repo cloned at the
  base commit into ``/workspace``. The sandbox lives for the duration of
  one eval sample and is torn down when the solver returns.
- DeepAgents uses its **native** sandbox-aware tool surface
  (``ls`` / ``read_file`` / ``glob`` / ``grep`` / ``write_file`` /
  ``edit_file`` / ``execute``) wired through
  :class:`evals.sandboxes.modal_backend.DockerSandboxBackend`. There is no
  bridge to Inspect's per-sample sandbox — agent execution is fully
  decoupled from the official benchmark Docker harness.
- The solver captures ``git diff`` from the Modal sandbox at the end of
  the run and emits an :class:`evals.common.predictions.SweBenchPrediction`
  to ``state.metadata['prediction']``. The
  :func:`evals.common.prediction_export.prediction_exporter` scorer then
  appends it to ``evals/outputs/.../*.predictions.jsonl`` in the official
  SWE-bench format.

The official ``run_evaluation.py`` (Docker harness) is run **offline** by
the user against the assembled JSONL — see the module-level docstring of
:mod:`evals.common.prediction_export` for the exact command.
"""

from __future__ import annotations

import logging
import textwrap
from typing import Any

from inspect_ai.solver import Generate, Solver, TaskState, solver

from evals.common.deepagents_baseline import (
    build_baseline_agent,
    build_run_config,
    resolve_model,
)
from evals.common.predictions import SweBenchPrediction, empty_swe_prediction
from evals.sandboxes import DockerSandboxBackend, RepoSpec

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = textwrap.dedent(
    """\
    You are an expert software engineer fixing a single GitHub issue in a
    checked-out repository inside a Linux sandbox. The repo is at
    /workspace (HEAD already detached at the issue's base commit; you are
    on a clean branch called ``openbot-agent``).

    Tools (sandbox-aware, all single round-trip):
      - ls / read_file / glob / grep — read
      - write_file (new file) / edit_file (in-place exact-string replace,
        replace_all=True for multiple occurrences) — write
      - execute — `bash -lc` escape hatch for git, pytest, patch -p1, etc.

    Loop:
      1. Read the issue carefully.
      2. Use grep / glob to locate the relevant code.
      3. Use read_file to understand context (request only the page you
         need via offset/limit — files are big).
      4. Use edit_file / write_file to apply the minimal fix; prefer them
         over execute+sed.
      5. Optionally re-run the failing test via execute to confirm green.
      6. Stop and reply with a one-paragraph summary.

    Run independent tool calls in parallel (e.g., reading 3 files at once
    is one round-trip, not three). Do NOT git commit, push, or switch
    branches — the grader captures your changes via `git diff` against
    HEAD when you finish.
    """
)


def _extract_text(message: Any) -> str:
    text = message.content if hasattr(message, "content") else str(message)
    if isinstance(text, list):
        text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
    return str(text)


def _extract_provider_usage(message: Any) -> dict[str, Any] | None:
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        return dict(usage)
    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, dict):
        nested = response_metadata.get("usage")
        if isinstance(nested, dict):
            return dict(nested)
    return None


def deepagents_baseline_swe_solver(*, model: str | None = None) -> Solver:
    """Inspect ``@solver`` driving deepagents on an SWE-bench sample.

    Builds one fresh Modal sandbox per sample (so per-sample state is
    isolated and the sandbox can be torn down when the solver returns).
    The captured ``git diff`` lands on ``state.metadata['prediction']``
    as a :class:`SweBenchPrediction`; the prediction exporter scorer
    serialises it to JSONL.
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
                # No usable checkout — emit empty prediction so the JSONL row
                # still validates and the failure is visible downstream.
                state.metadata["prediction"] = empty_swe_prediction(
                    instance_id=instance_id,
                    model_name_or_path=resolved_model,
                ).model_dump()
                state.output.completion = (
                    "ERROR: sample missing repo/base_commit metadata; emitted empty prediction."
                )
                return state

            backend = await DockerSandboxBackend.create_for_sample(
                repo_spec=RepoSpec(repo=repo, base_commit=base_commit),
            )
            try:
                agent = build_baseline_agent(
                    system_prompt=_SYSTEM_PROMPT,
                    model=resolved_model,
                    backend=backend,
                )
                user_msg = (
                    "Fix the following GitHub issue by modifying the "
                    "repository code in place. The repo is at /workspace.\n\n"
                    f"<issue>\n{state.input_text}\n</issue>"
                )
                ls_config = build_run_config(
                    sample_id=instance_id,
                    dataset_version=md.get("dataset_version", "fix_swe_bench_verified"),
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

                patch = await backend.acapture_diff()
                prediction = SweBenchPrediction(
                    instance_id=instance_id,
                    model_name_or_path=resolved_model,
                    model_patch=patch,
                )
                state.metadata["prediction"] = prediction.model_dump()
                state.metadata["modal_sandbox_id"] = backend.id

                last = result["messages"][-1]
                provider_usage = _extract_provider_usage(last)
                if provider_usage is not None:
                    state.metadata["provider_usage"] = provider_usage
                state.output.completion = _extract_text(last)
            finally:
                await backend.aclose()
            return state

        return _run

    return _solver()
