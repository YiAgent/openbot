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
from evals.common.termination import assert_clean_termination
from evals.common.usage import aggregate_provider_usage
from evals.sandboxes import RepoSpec, create_sandbox_for_sample

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = textwrap.dedent(
    """\
    ROLE: You are a senior software engineer fixing one GitHub issue in
    an already-checked-out repository.

    GOAL: Modify the source code in /workspace so the bug described in the
    issue is fixed. The grader runs the project's pre-existing test suite
    against your code (offline, after your run) — your job is to produce
    a minimal patch that makes the failing test(s) pass without breaking
    any passing test.

    SUCCESS CRITERIA:
      - The smallest set of file edits that fixes the issue.
      - No new tests, no refactoring beyond what the fix needs.
      - No git commit, push, branch switch — the grader captures changes
        via ``git diff`` against the base commit at end of run.

    ENVIRONMENT:
      - /workspace: repo at the issue's base commit (HEAD detached, branch
        ``openbot-agent``).
      - Tools (sandbox-aware, all single round-trip):
        · ls / read_file / glob / grep — read
        · write_file / edit_file (exact-string replace, replace_all=True
          for repeated occurrences) — write
        · execute — ``bash -lc`` escape hatch for git/pytest/patch.
      - Use the plan tool (write_todos) to track what you're investigating
        and what you've fixed. It's the easiest way to avoid the
        "let me check one more thing" failure mode.

    WORKFLOW:
      1. Read the issue body and form a hypothesis: what file(s) is the
         bug likely in, what is the expected behaviour, what changed?
      2. Locate the buggy code via grep / glob + a targeted read_file.
         Read only the page you need (offset/limit) — files can be huge.
      3. Apply the minimal fix with edit_file. Prefer edit_file over
         execute+sed (safer, structured response).
      4. Optionally run the failing test via execute to sanity-check.
         If the test is unknown, just trust your reasoning and stop.
      5. Stop. Reply with a one-paragraph summary of the change.

    DISCIPLINE:
      - Run independent tool calls in parallel (3 reads = 1 round-trip).
      - Cap at ~10 tool calls before you must commit a fix. The vast
        majority of fixes need ≤5 reads + 1-2 edits.
      - Do NOT say "let me explore the codebase first" without a
         specific hypothesis — start from the issue text.
    """
)


def _extract_text(message: Any) -> str:
    text = message.content if hasattr(message, "content") else str(message)
    if isinstance(text, list):
        text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
    return str(text)


# Usage aggregation lives in evals.common.usage. We sum across all AI
# messages because LangChain attaches per-call usage to each message and
# the agent loop produces many — see that module's docstring.


def _join_message_text(messages: list[Any]) -> str:
    """Concatenate all AI-visible message text for offline debugging."""
    parts = [_extract_text(message).strip() for message in messages]
    return "\n\n".join(part for part in parts if part)


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

            backend = await create_sandbox_for_sample(
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

                # Refuse to capture / score a diff from a run the agent
                # never finished cleanly (middleware-cut, rate-limit storm,
                # pending tool-call, empty final message). Raising here
                # marks the sample as errored — Inspect excludes it from
                # the metric and from the predictions JSONL, instead of
                # shipping an empty patch that would be misread as
                # "model failed to fix".
                assert_clean_termination(result, requires_structured_response=False)
                patch = await backend.acapture_diff()
                prediction = SweBenchPrediction(
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
                state.metadata["agent_raw_output"] = _join_message_text(messages)
                # Stable eval/export surface: the benchmark output is the
                # prediction patch, not the agent's final prose.
                state.output.completion = prediction.model_dump_json()
            finally:
                await backend.aclose()
            return state

        return _run

    return _solver()
