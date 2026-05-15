"""Inspect solver wrapping a deepagents-based PR review workflow — PRD §4.1 / §6.2.

**Temporary v0.1 implementation.** PRD §4.1 specifies the production review
workflow lives at `openbot.workflows.review.run(...)`. That module hasn't
shipped yet (v0.1 Week 1 skeleton). To unblock baseline measurement, this
solver uses [deepagents](https://github.com/langchain-ai/deepagents) — a
LangGraph-based deep-agent framework — as a stand-in.

When the real `openbot.workflows.review` lands, this file should swap its
internal `review_diff` call for `openbot.workflows.review.run` and preserve
the same input/output contract:
  - Input  : PR diff (str)
  - Output : list[Finding] where Finding = {file, line: int | None, body, severity}

The Inspect AI `@solver` shim at the bottom is the entry point used by
`evals/tasks/review_martian.py`.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Literal, TypedDict

from deepagents import create_deep_agent

# eval PRD §17 #5 locks `anthropic:claude-opus-4-7` as the canonical review
# model. The env override exists for v0.1 dev (using GLM via the
# Anthropic-compatible endpoint when an Anthropic API key isn't on hand) and
# for future multi-model regression sweeps. Production gates still pin opus.
_DEFAULT_REVIEW_MODEL_ID = os.environ.get("OPENBOT_REVIEW_MODEL_ID", "anthropic:claude-opus-4-7")
# If the value is a bare model name (e.g. "glm-5.1"), prefix with the
# anthropic: provider so deepagents/langchain routes through the Anthropic
# client (which then honors ANTHROPIC_BASE_URL).
if ":" not in _DEFAULT_REVIEW_MODEL_ID:
    _DEFAULT_REVIEW_MODEL_ID = f"anthropic:{_DEFAULT_REVIEW_MODEL_ID}"

_REVIEW_SYSTEM_PROMPT = """\
You are an experienced code reviewer. Read the PR diff carefully and report
real defects, bugs, security issues, or correctness problems.

Output rules:
- ALWAYS respond with a single JSON object on the LAST line, with key "findings".
- Each finding has: {"file": str, "line": int|null, "body": str, "severity": "low"|"medium"|"high"}.
- "file" must be a path that appears in the diff (after b/ prefix).
- "line" is the 1-indexed new-file line if you can pin it; null otherwise.
- "body" is one sentence; no markdown, no chain-of-thought.
- "severity": high = will break / vulnerability; medium = real bug or risk; low = nit / style.
- If the diff is clean, return {"findings": []}. Do NOT invent findings.

Example response:
{"findings": [{"file": "src/auth.py", "line": 42, "body": "Token comparison is not constant-time; vulnerable to timing attack.", "severity": "high"}]}
"""


class Finding(TypedDict):
    """Output shape — also referenced by `evals.scorers.review_overlap.Finding`."""

    file: str
    line: int | None
    body: str
    severity: Literal["low", "medium", "high"]


def _normalize_severity(value: Any) -> Literal["low", "medium", "high"]:
    s = str(value).lower().strip()
    if s in {"low", "medium", "high"}:
        return s  # type: ignore[return-value]
    return "medium"


def _coerce_findings(raw: Any) -> list[Finding]:
    """Best-effort: pull a `findings` list out of whatever the agent returned."""
    if isinstance(raw, dict) and "findings" in raw:
        items = raw["findings"]
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    out: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                {
                    "file": str(item["file"]),
                    "line": int(item["line"]) if item.get("line") is not None else None,
                    "body": str(item["body"]),
                    "severity": _normalize_severity(item.get("severity", "medium")),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the LAST JSON object from `text` — agents sometimes prepend prose."""
    matches = re.findall(r"\{(?:[^{}]|\{[^{}]*\})*\}", text, flags=re.DOTALL)
    for blob in reversed(matches):
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def review_diff(diff: str, *, model: str = _DEFAULT_REVIEW_MODEL_ID) -> list[Finding]:
    """Run the deep agent on a PR diff, return normalized findings.

    Pure function (no inspect-ai imports) so it's directly callable in tests.
    """
    agent = create_deep_agent(
        model=model,
        tools=[],  # no shell / file tools — review is closed-form on the diff text
        system_prompt=_REVIEW_SYSTEM_PROMPT,
    )
    user_msg = f"Review this PR diff:\n\n```diff\n{diff}\n```"
    result = agent.invoke({"messages": [{"role": "user", "content": user_msg}]})

    last_msg = result["messages"][-1]
    text = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
    if isinstance(text, list):
        # Anthropic content blocks: [{"type": "text", "text": "..."}, ...]
        text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))

    obj = _extract_json_object(str(text))
    return _coerce_findings(obj) if obj else []


# ─── Inspect AI @solver shim ────────────────────────────────────────────────


def openbot_review_solver():  # type: ignore[no-untyped-def]
    """Inspect AI `@solver` — wraps `review_diff` for the runner.

    The Inspect Sample's `input` is the PR diff (str). The solver writes the
    findings list into `state.metadata["candidate_findings"]` so downstream
    scorers (E1-T07 review_overlap) can read it.
    """
    from inspect_ai.solver import Generate, Solver, TaskState, solver

    @solver
    def _solver() -> Solver:
        async def _run(state: TaskState, _generate: Generate) -> TaskState:
            diff = state.input_text
            findings = review_diff(diff)
            state.metadata["candidate_findings"] = findings
            state.output.completion = json.dumps({"findings": findings}, ensure_ascii=False)
            return state

        return _run

    return _solver()
