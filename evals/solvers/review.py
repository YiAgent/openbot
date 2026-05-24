"""Inspect solver wrapping the production OpenBot review path (PRD §4.1).

Rewritten in the evals-runtime-redesign to call ``openbot.evaluation.run_review_sample``
instead of the old ``evals.agents.*`` + ``evals.sandboxes.*`` stack.

This means evals now measure the production code path end-to-end.

Public surface kept stable:
  - ``Finding`` TypedDict — the scorer (review_overlap.py) reads this shape.
  - ``state.metadata["candidate_findings"]`` — list[Finding] for the scorer.
  - ``state.metadata["candidate_findings_json"]`` — JSON string for trace export.
  - ``state.metadata["agent_raw_output"]`` — summary text from ReviewFindings.
"""

from __future__ import annotations

import json
from typing import Literal, TypedDict

from openbot.evaluation import run_review_sample


class Finding(TypedDict):
    """Output shape — also referenced by ``evals.scorers.review_overlap.Finding``."""

    file: str
    line: int | None
    body: str
    severity: Literal["low", "medium", "high"]


def _domain_findings_to_eval(
    findings: tuple,
) -> list[Finding]:
    """Convert domain Finding objects to the eval Finding dict format.

    Mappings:
      domain.Finding.message  → eval Finding.body
      domain severity "nit"   → eval severity "low"
    """
    result: list[Finding] = []
    for f in findings:
        sev = str(f.severity)
        # "nit" is a valid domain severity; scorers only know low/medium/high
        if sev == "nit":
            sev = "low"
        result.append(
            {
                "file": f.file,
                "line": f.line,
                "body": f.message,
                "severity": sev,
            }
        )
    return result


def openbot_review_solver(
    *,
    model: str | None = None,
):
    """Inspect AI solver shim — calls the production review responder.

    Args:
        model: Reserved for future model-routing hooks. Currently unused;
               the production responder reads its model from Settings.

    State keys written:
      - ``candidate_findings``      list[Finding] for the overlap scorer.
      - ``candidate_findings_json`` JSON string for trace export.
      - ``agent_raw_output``        summary text from ReviewFindings.
    """
    try:
        from inspect_ai.solver import solver
    except ImportError:
        # inspect_ai is an optional eval dependency; defer the ImportError
        # to call time so unit tests can import this module without it.
        def solver(fn):  # type: ignore[assignment]
            return fn

    @solver
    def _solver():
        async def _run(state, _generate):
            md = state.metadata or {}
            diff = state.input_text or ""
            repo = str(md.get("repo", "unknown/repo"))
            pr_number = int(md.get("pr_number") or 0)
            sample_id = str(state.sample_id) if state.sample_id is not None else "anon"

            findings_obj = await run_review_sample(
                repo=repo,
                pr_number=pr_number,
                pr_diff=diff,
                run_id=sample_id,
            )

            eval_findings = _domain_findings_to_eval(findings_obj.findings)
            findings_json = json.dumps({"findings": eval_findings}, ensure_ascii=False)

            state.metadata["candidate_findings"] = eval_findings
            state.metadata["candidate_findings_json"] = findings_json
            state.metadata["agent_raw_output"] = findings_obj.summary
            state.output.completion = findings_json
            return state

        return _run

    return _solver()


# Backward-compat alias — tasks that imported the old solver name
# (deepagents_baseline_review_solver) can import this without changing
# their call site.
deepagents_baseline_review_solver = openbot_review_solver

__all__ = [
    "Finding",
    "_domain_findings_to_eval",
    "deepagents_baseline_review_solver",
    "openbot_review_solver",
]
