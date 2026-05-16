"""SWE-QA-Pro solvers — baseline (no sandbox) + +Agent (Modal sandbox).

SWE-QA-Pro (TIGER-Lab, arXiv 2603.16124) evaluates *repository-level* code
understanding. The paper reports two columns per model: direct answering vs
an agent loop with file inspection tools (Table 2). This module exposes
both variants behind a uniform surface:

- :func:`deepagents_baseline_swe_qa_solver` — closed-book direct answer.
  No tools, no sandbox. Maps to the paper's "Claude Sonnet 4.5: 27.69"
  baseline row.
- :func:`deepagents_agent_swe_qa_solver` — paper "+Agent" variant. Each
  sample runs in its own **Modal sandbox** with the target repo cloned at
  the pinned commit into ``/workspace``. The agent uses deepagents'
  native sandbox-aware tools (``ls`` / ``glob`` / ``grep`` / ``read_file``
  / ``execute``) via :class:`DockerSandboxBackend`. Final answer is
  schema-bound to :class:`SweQaProAnswer` so the judge always sees a
  well-formed body + structured citations regardless of how the agent
  formats its prose.

The agent sandbox here is fully separate from the evaluation sandbox of
any other benchmark — there is no Docker/Inspect sandbox in this flow.
"""

from __future__ import annotations

import re
import textwrap
from typing import Any

from evals.common.deepagents_baseline import (
    build_baseline_agent,
    build_run_config,
    resolve_model,
)
from evals.common.predictions import SweQaProAnswer, SweQaProCitation
from evals.sandboxes import DockerSandboxBackend, RepoSpec

_DEFAULT_FALLBACK = "anthropic:claude-sonnet-4-6"

_BASELINE_SYSTEM_PROMPT = textwrap.dedent(
    """\
    You are an expert software engineer answering repository-level questions
    about a specific open-source codebase. The user will give you a question
    and identify the repository at a specific commit; answer as faithfully
    as possible from your knowledge of that repository.

    Answer requirements:
      - Cite concrete file paths (e.g. ``src/foo/bar.py``) when discussing
        specific code. Include line numbers where you have high confidence.
      - Prefer specificity over hedging: name classes, functions, decorators,
        config keys, CLI flags rather than describing them abstractly.
      - If you genuinely do not know, say so briefly; do not fabricate file
        paths or symbols. Hallucinated citations are scored harshly.
      - Structure: one short overview paragraph, then a bulleted list of the
        concrete components / files involved. No markdown headings.

    Do NOT include chain-of-thought. The grader scores only the final answer.
    """
)


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
You MUST follow this output structure at each assitant turn:
1. Reasoning
* Before any tool call, output only your step by step planning explanation
2. Final Answer
* Output **exactly one** block in this format without any tool calls:
<finish>
{Final answer's content}
</finish>
Rules for '<finish>' block:
* Must appear exactly once.
* Must contain only the final answer's content.
* NO code blocks or copied code such as "'python ...''.
* Cite evidence only using file paths relative to repo_path, in the format <relative_path>: line <start>-<end> (do not use absolute paths) e.g. responses/init.py: line 1-10.
Any violation of this protocol makes the answer invalid.
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
5) Provide a complete answer with evidence inside a <finish> block"""

# ─── End verbatim block ───────────────────────────────────────────────────


_FINISH_RE = re.compile(r"<finish>(.*?)</finish>", re.DOTALL)
_CITATION_RE = re.compile(
    r"(?P<path>[\w./\-]+):\s*line\s*(?P<start>\d+)-(?P<end>\d+)",
    re.IGNORECASE,
)


def _extract_text(message: Any) -> str:
    text = message.content if hasattr(message, "content") else str(message)
    if isinstance(text, list):
        text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
    return str(text)


def _extract_finish_block(text: str) -> str:
    """Pull the answer out of ``<finish>...</finish>``.

    Per Appendix D the agent MUST end with one ``<finish>`` block; we take
    the last one as conservative fallback (some models emit early drafts).
    If absent, return the raw text so the judge still has something to
    score — the protocol violation will surface in the score itself.
    """
    matches = _FINISH_RE.findall(text)
    if not matches:
        return text.strip()
    return matches[-1].strip()


def _parse_citations(text: str) -> list[dict[str, Any]]:
    """Parse ``<relative_path>: line <s>-<e>`` references out of ``text``.

    Best-effort: we only return citations that look syntactically valid.
    The judge does not consume this — it's our offline quality signal
    (citation-rate, hallucinated-path-rate).
    """
    citations: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for m in _CITATION_RE.finditer(text):
        start = int(m.group("start"))
        end = int(m.group("end"))
        if end < start:
            continue
        key = (m.group("path"), start, end)
        if key in seen:
            continue
        seen.add(key)
        citations.append({"relative_path": m.group("path"), "line_start": start, "line_end": end})
    return citations


# ─── Baseline: no tools, no sandbox ────────────────────────────────────────


async def _invoke_baseline_agent(
    *,
    question: str,
    repo: str,
    commit_id: str,
    model: str,
) -> dict[str, Any]:
    """Run the closed-book deepagents call. Returns text + usage + run_id."""
    from langsmith.run_helpers import get_current_run_tree

    agent = build_baseline_agent(
        model=model,
        tools=[],
        system_prompt=_BASELINE_SYSTEM_PROMPT,
    )
    header = f"Repository: {repo}\nCommit: {commit_id}\n\n" if repo else ""
    user_msg = f"{header}Question: {question}"
    result = await agent.ainvoke({"messages": [{"role": "user", "content": user_msg}]})
    last = result["messages"][-1]
    text = _extract_text(last)
    usage = getattr(last, "usage_metadata", None)
    run = get_current_run_tree()
    return {
        "text": text,
        "usage": dict(usage) if isinstance(usage, dict) else None,
        "langsmith_run_id": str(run.id) if run is not None else None,
    }


def deepagents_baseline_swe_qa_solver(*, model: str | None = None):  # type: ignore[no-untyped-def]
    """Inspect ``@solver`` — deepagents closed-book direct-answer baseline.

    Wraps each sample in a named LangSmith ``traceable`` root span so the
    project view shows one row per Inspect sample. No tools, no sandbox —
    just the model and the question text.
    """
    from inspect_ai.solver import Generate, Solver, TaskState, solver
    from langsmith import traceable

    resolved_model = resolve_model(
        override=model,
        fallback=_DEFAULT_FALLBACK,
    )

    @solver
    def _solver() -> Solver:
        async def _run(state: TaskState, _generate: Generate) -> TaskState:
            md = state.metadata or {}
            sample_id = str(state.sample_id) if state.sample_id is not None else ""
            repo = str(md.get("repo", ""))
            commit_id = str(md.get("commit_id", ""))
            dataset_version = str(md.get("dataset_version", "chat_swe_qa_pro"))
            traced = traceable(
                name="chat_swe_qa_pro_baseline.sample",
                run_type="chain",
                tags=["chat_swe_qa_pro", "deepagents_baseline", "direct_answer"],
                metadata={
                    "sample_id": sample_id,
                    "repo": repo,
                    "commit_id": commit_id,
                    "qa_class": str(md.get("qa_class", "")),
                    "qa_subclass": str(md.get("qa_subclass", "")),
                    "cluster_id": str(md.get("cluster_id", "")),
                    "model_id": resolved_model,
                    "dataset_version": dataset_version,
                    "dataset_sha256": str(md.get("dataset_sha256", "")),
                },
            )(_invoke_baseline_agent)
            out = await traced(
                question=state.input_text,
                repo=repo,
                commit_id=commit_id,
                model=resolved_model,
            )

            if out["usage"] is not None:
                state.metadata["provider_usage"] = out["usage"]
            if out["langsmith_run_id"]:
                state.metadata["langsmith_run_id"] = out["langsmith_run_id"]
            state.output.completion = out["text"]
            return state

        return _run

    return _solver()


# ─── +Agent variant: Modal sandbox with repo cloned at commit ──────────────

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
    """Run the +Agent flow: spin up Modal, run deepagents, capture answer.

    The agent's response is parsed for the Appendix D ``<finish>`` block,
    repackaged into a :class:`SweQaProAnswer`, and returned alongside
    LangSmith trace metadata for the surrounding solver.
    """
    from langsmith.run_helpers import get_current_run_tree

    backend = await DockerSandboxBackend.create_for_sample(
        repo_spec=RepoSpec(repo=repo, base_commit=commit_id, workspace=repo_path),
    )
    try:
        agent = build_baseline_agent(
            system_prompt=SWE_QA_PRO_PAPER_AGENT_SYSTEM,
            model=model,
            backend=backend,
        )
        user_msg = SWE_QA_PRO_PAPER_AGENT_USER_TEMPLATE.format(
            repo_path=repo_path,
            question=question,
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_msg}]},
            config=ls_config,
        )
        last = result["messages"][-1]
        raw = _extract_text(last)
        answer_body = _extract_finish_block(raw)
        usage = getattr(last, "usage_metadata", None)
        run = get_current_run_tree()
        structured = SweQaProAnswer(
            answer=answer_body,
            citations=[SweQaProCitation(**c) for c in _parse_citations(answer_body)],
        )
        return {
            "text": answer_body,
            "raw_text": raw,
            "structured": structured.model_dump(),
            "usage": dict(usage) if isinstance(usage, dict) else None,
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

    resolved_model = resolve_model(
        override=model,
        fallback=_DEFAULT_FALLBACK,
    )

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
            state.metadata["agent_raw_output"] = out["raw_text"]
            state.metadata["prediction"] = out["structured"]
            state.output.completion = out["text"]
            return state

        return _run

    return _solver()
