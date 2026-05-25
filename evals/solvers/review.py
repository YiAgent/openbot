"""Inspect solver wrapping the production OpenBot review path (PRD §4.1).

Calls ``openbot.evaluation.run_review_sample`` which exercises the real
production review workflow end-to-end.

Public surface kept stable:
  - ``Finding`` TypedDict — the scorer (review_overlap.py) reads this shape.
  - ``state.metadata["candidate_findings"]`` — list[Finding] for the scorer.
  - ``state.metadata["candidate_findings_json"]`` — JSON string for trace export.
  - ``state.metadata["agent_raw_output"]`` — summary text from ReviewFindings.

File-fetching design:
  The solver creates a ``GitHubFileReader`` from the sample's ``pr_url`` and
  ``base_sha`` metadata and injects it into the eval runner.  This lets the
  review agent's ``read_file`` / ``grep_repo`` tool calls hit the real GitHub
  API at the correct base commit, without duplicating file-fetching logic in
  the Inspect AI eval layer (which would violate the decoupling principle).
"""

from __future__ import annotations

import json
import re
from typing import Literal, TypedDict

from openbot.evaluation import run_review_sample

# Pattern: https://github.com/<owner>/<repo>/pull/<number>
_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


class Finding(TypedDict):
    """Output shape — also referenced by ``evals.scorers.review_overlap.Finding``."""

    file: str
    line: int | None
    body: str
    severity: Literal["low", "medium", "high"]


def _parse_github_repo(pr_url: str) -> tuple[str, int] | None:
    """Parse ``pr_url`` into ``(owner/repo, pr_number)`` or ``None`` if unrecognised."""
    m = _PR_URL_RE.match(str(pr_url or ""))
    if m is None:
        return None
    owner = m.group("owner")
    repo = m.group("repo")
    number = int(m.group("number"))
    return f"{owner}/{repo}", number


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
            sample_id = str(state.sample_id) if state.sample_id is not None else "anon"

            # Parse owner/repo and pr_number from pr_url; fall back to metadata
            # fields when pr_url is absent (e.g. in unit tests).
            pr_url = str(md.get("pr_url") or "")
            parsed = _parse_github_repo(pr_url)
            if parsed is not None:
                github_repo, pr_number = parsed
            else:
                # Legacy / test path: use the metadata fields directly.
                github_repo = str(md.get("repo", "unknown/repo"))
                pr_number = int(md.get("pr_number") or 0)

            # Build a live file reader when we have enough metadata to do so.
            # This lets the agent's read_file / grep_repo tool calls hit the
            # real GitHub API at the correct base commit rather than receiving
            # empty strings from the default in-memory file map.
            file_reader = None
            base_sha = str(md.get("base_sha") or "")
            if github_repo and base_sha:
                from openbot.evaluation.github_file_reader import GitHubFileReader

                file_reader = GitHubFileReader(repo=github_repo, ref=base_sha)

            findings_obj = await run_review_sample(
                repo=github_repo,
                pr_number=pr_number,
                pr_diff=diff,
                run_id=sample_id,
                file_reader=file_reader,
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


__all__ = [
    "Finding",
    "_domain_findings_to_eval",
    "_parse_github_repo",
    "openbot_review_solver",
]
