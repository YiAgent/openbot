"""Preconfigured DeepAgents agent for SWE-QA-Pro repo-QA evals."""

from __future__ import annotations

from typing import Any

from evals.agents.baseline import build_baseline_agent
from evals.common.predictions import SweQaProAnswer

SWE_QA_PRO_PAPER_AGENT_SYSTEM: str = """You are a codebase analysis agent operating in a strictly read-only environment.
Your task is to answer SWE-related questions by analyzing source code, configuration, documentation, and tests.
You must prioritize correctness, completeness, clarity, relevance and evidence-based reasoning when answering given questions within 25 max turns.
For every question, inspect the repository, search relevant files, examine implementations, cross-validate findings, and then emit the final answer using the structured response binding.
The structured response has two fields:
- answer: natural-language answer body. NO copied code blocks.
- citations: evidence objects with relative_path, line_start, and line_end."""

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


def build_chat_agent(*, model: str, backend: Any):
    """Build the SWE-QA-Pro +Agent repo-QA agent from the shared baseline agent."""
    return build_baseline_agent(
        system_prompt=SWE_QA_PRO_PAPER_AGENT_SYSTEM,
        model=model,
        backend=backend,
        response_format=SweQaProAnswer,
    )


def build_chat_user_message(*, question: str, repo_path: str) -> str:
    """Build the user message for one SWE-QA-Pro question."""
    return SWE_QA_PRO_PAPER_AGENT_USER_TEMPLATE.format(
        repo_path=repo_path,
        question=question,
    )
