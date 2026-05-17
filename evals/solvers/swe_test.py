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
from evals.common.termination import assert_clean_termination
from evals.common.usage import aggregate_provider_usage
from evals.sandboxes import RepoSpec, create_sandbox_for_sample

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = textwrap.dedent(
    """\
    ROLE: You are a senior software engineer writing a regression test
    for one GitHub issue.

    GOAL: Add ONE pytest test (or a small new ``test_*.py`` file) that
    FAILS on the current buggy code and would PASS once the issue is
    fixed. This is the inverse of the fix task: production code stays
    untouched; the diff must be test-only.

    SUCCESS CRITERIA:
      - Exactly one new failing test, in the project's existing test
        layout (use ``glob`` / ``ls`` to find where similar tests live;
        mirror that location and import style).
      - No edits to production code. Any non-test file modification will
        be rejected by the grader.
      - The test fails for the right reason — i.e., it triggers the
        actual buggy behaviour described in the issue, not an unrelated
        ImportError or fixture problem.
      - No git commit, push, branch switch. Grader captures via
        ``git diff``.

    ENVIRONMENT:
      - /workspace: repo at the issue's base commit. No network, no new
        deps — work with whatever's already installable.
      - Tools (sandbox-aware, single round-trip each):
        · ls / read_file / glob / grep — read
        · write_file (new test file) / edit_file (extend existing test
          module — exact-string replace, replace_all=True for repeats)
        · execute — ``bash -lc`` for ``pytest -k …`` iteration only.
      - Use the plan tool (write_todos) to track: locate the bug, find
        the test home, draft the test, verify it fails for the right
        reason.

    WORKFLOW:
      1. Read the issue. Identify the exact buggy behaviour to trigger
         (input → wrong output, exception path, etc.).
      2. Locate the production code that owns this behaviour via
         grep / glob.
      3. Locate the existing test file for that production code (search
         ``tests/`` or ``test_*.py`` near the production module).
      4. Read enough of the existing tests to copy conventions
         (fixtures, imports, helpers).
      5. Write the new test with write_file or extend the existing
         module with edit_file. ONE test, minimal asserts.
      6. Optionally run ``pytest -k <new_test_name>`` via execute to
         confirm it fails — but a confident write without verification
         is OK if execute is slow.
      7. Stop. Reply with a one-paragraph summary.

    DISCIPLINE:
      - Run independent tool calls in parallel (3 reads = 1 round-trip).
      - Cap at ~10 tool calls before you must commit the test. Most
        regression tests need ≤5 reads + 1 write.
      - Do NOT modify production code "just to see what happens" — the
        grader rejects any production-code change in the diff.
      - Do NOT write multiple tests. ONE focused failure is the contract.
    """
)


def _extract_text(message: Any) -> str:
    text = message.content if hasattr(message, "content") else str(message)
    if isinstance(text, list):
        text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
    return str(text)


# Usage aggregation lives in evals.common.usage — sums across all AI
# messages, matching LangSmith's trace-side aggregation.


def _join_message_text(messages: list[Any]) -> str:
    """Concatenate all AI-visible message text for offline debugging."""
    parts = [_extract_text(message).strip() for message in messages]
    return "\n\n".join(part for part in parts if part)


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

                # Same termination contract as swe_fix: refuse to capture
                # a diff (and write a prediction row) from a run the agent
                # never finished cleanly. Raising marks the sample errored
                # so it skips both the metric and the predictions JSONL.
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
                state.metadata["agent_raw_output"] = _join_message_text(messages)
                state.output.completion = prediction.model_dump_json()
            finally:
                await backend.aclose()
            return state

        return _run

    return _solver()
