"""Preconfigured DeepAgents agent for SWT-Bench test-generation evals."""

from __future__ import annotations

import textwrap
from typing import Any

from evals.agents.baseline import build_baseline_agent

TEST_GENERATION_SYSTEM_PROMPT = textwrap.dedent(
    """\
    ROLE: You are a senior software engineer writing a regression test
    for one GitHub issue.

    GOAL: Add ONE pytest test that FAILS on the current buggy code and would
    PASS once the issue is fixed. Production code stays untouched; the diff
    must be test-only.

    SUCCESS CRITERIA:
      - Exactly one focused failing test in the project's existing test layout.
      - No production-code edits.
      - No git commit, push, or branch switch.

    ENVIRONMENT:
      - /workspace: repo at the issue's base commit.
      - Tools: ls / read_file / glob / grep / write_file / edit_file / execute.

    WORKFLOW:
      1. Read the issue and identify the buggy behavior to trigger.
      2. Locate the owning production code and nearby tests.
      3. Write one test using existing conventions.
      4. Optionally run the focused pytest command to confirm the failure.
      5. Stop with a one-paragraph summary.
    """
)


def build_test_generation_agent(*, model: str, backend: Any):
    """Build the SWT-Bench test-generation agent from the shared baseline agent."""
    return build_baseline_agent(
        system_prompt=TEST_GENERATION_SYSTEM_PROMPT,
        model=model,
        backend=backend,
    )


def build_test_generation_user_message(issue: str) -> str:
    """Build the user message for one SWT-Bench issue."""
    return (
        "Write a single regression pytest test (or a new test_*.py file) that "
        "FAILS on the current buggy code and would PASS once the following "
        "GitHub issue is fixed. Only edit test files.\n\n"
        f"<issue>\n{issue}\n</issue>"
    )
