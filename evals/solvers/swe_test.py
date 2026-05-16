"""SWT-Bench Verified solver — deepagents inside an isolated Modal sandbox.

Sister to :mod:`evals.solvers.swe_fix`. The differences:

- **Prompt** asks for a regression test only — production code is off
  limits. The official SWT-Bench grader rejects any patch that touches
  non-test files, so the prompt is the soft front-line of that contract.
- **Output schema** is :class:`evals.common.predictions.SwtBenchPrediction`.
  SWT-Bench's ``predictions.jsonl`` reuses the SWE-bench shape verbatim
  (the ``model_patch`` field carries the test-only diff).
- **No tests are run here.** This solver only captures the agent's diff
  via ``git diff`` and writes it to ``state.metadata['prediction']`` for
  the prediction exporter scorer. Actual grading happens **offline**
  against the official SWT-Bench Docker harness — see
  :mod:`evals.common.prediction_export`.
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
from evals.common.predictions import SwtBenchPrediction, empty_swt_prediction
from evals.sandboxes import DockerSandboxBackend, RepoSpec

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = textwrap.dedent(
    """\
    You are an expert software engineer writing a regression test for a
    single GitHub issue in a checked-out repository inside a Linux sandbox.
    The repo is at /workspace (HEAD detached at the issue's base commit;
    you are on a clean branch ``openbot-agent``).

    Your goal:
      - Write ONE pytest test (or a small new ``test_*.py`` file) that
        **FAILS** on the current buggy code and would **PASS** once the
        issue is fixed.
      - Do NOT modify production code. The test must be the only thing
        that changes. Edits to anything outside the project's test
        directory are rejected by the grader.
      - Land the test in the project's existing test layout. Use ``glob``
        and ``ls`` to find where similar tests already live; mirror that
        location and import style.

    Tools (sandbox-aware, all single round-trip):
      - ls / read_file / glob / grep — read
      - write_file (new file) / edit_file (in-place exact-string replace,
        replace_all=True for multiple occurrences) — write
      - execute — `bash -lc` escape hatch for running pytest while you
        iterate. Do not git commit, switch branches, or install new deps.

    Loop:
      1. Read the issue carefully and identify the buggy behaviour the
         test must trigger.
      2. Use grep / glob to locate the relevant code and the matching
         test file (search ``tests/`` / ``test_*.py``).
      3. Use read_file to understand the existing test layout (fixtures,
         imports, conventions).
      4. Use write_file (for a new test) or edit_file (to extend an
         existing test module) to add a single targeted test.
      5. Optionally run the new test via execute to confirm it fails
         for the right reason on the current code.
      6. Stop and reply with a one-paragraph summary.

    Run independent tool calls in parallel. The grader captures your
    changes via ``git diff`` when you finish.
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
                    "Write a single regression pytest test (or a new "
                    "test_*.py file) that FAILS on the current buggy code "
                    "and would PASS once the following GitHub issue is "
                    "fixed. Only edit test files.\n\n"
                    f"<issue>\n{state.input_text}\n</issue>"
                )
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

                patch = await backend.acapture_diff()
                prediction = SwtBenchPrediction(
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
