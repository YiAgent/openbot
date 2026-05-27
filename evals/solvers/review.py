"""Inspect solver wrapping the production OpenBot review path (PRD §4.1).

Calls ``openbot.evaluation.run_review_sample`` which exercises the real
production review workflow end-to-end.

Public surface kept stable:
  - ``Finding`` TypedDict — the scorer (review_overlap.py) reads this shape.
  - ``state.metadata["candidate_findings"]`` — list[Finding] for the scorer.
  - ``state.metadata["candidate_findings_json"]`` — JSON string for trace export.
  - ``state.metadata["agent_raw_output"]`` — summary text from ReviewFindings.

File-fetching design (unified sandbox pattern):
  The solver passes ``sandbox_factory`` (from ``build_sandbox_factory(settings)``)
  together with ``base_sha`` and ``clone_url`` to ``run_review_sample``.  The
  runner opens a Daytona sandbox, clones the repo at ``base_sha``, and wraps it
  in a ``SandboxFileReader`` so the review agent's ``read_file`` / ``grep_repo``
  tool calls read from the local clone — no live GitHub API dependency, no rate
  limits, deterministic at the exact commit.

  When no sandbox backend is configured (DAYTONA_API_KEY not set), the runner
  falls back to an empty file map; the agent reviews using the diff alone.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Literal, TypedDict

from openbot.application.sandbox_factory_deps import build_sandbox_factory
from openbot.core.settings import Settings
from openbot.evaluation import run_review_sample
from openbot.evaluation.runner import pop_agent_metadata

logger = logging.getLogger(__name__)

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

            # Build a sandbox factory for file access.
            # The runner opens a Daytona sandbox, clones the repo at base_sha,
            # and serves file reads from the local clone — no GitHub API needed.
            base_sha = str(md.get("base_sha") or "")
            clone_url = f"https://github.com/{github_repo}.git" if github_repo else ""
            settings = Settings()
            sandbox_factory = build_sandbox_factory(settings)
            if sandbox_factory is None:
                logger.warning(
                    "review_solver: no sandbox configured (DAYTONA_API_KEY not set); "
                    "review agent will work from diff alone (no file reads)."
                )

            findings_obj = await run_review_sample(
                repo=github_repo,
                pr_number=pr_number,
                pr_diff=diff,
                run_id=sample_id,
                sandbox_factory=sandbox_factory,
                base_sha=base_sha,
                clone_url=clone_url,
            )

            findings_list = _domain_findings_to_eval(findings_obj.findings)
            findings_json = json.dumps({"findings": findings_list}, ensure_ascii=False)

            state.metadata["candidate_findings"] = findings_list
            state.metadata["candidate_findings_json"] = findings_json
            state.metadata["agent_raw_output"] = findings_obj.summary
            state.output.completion = findings_json

            # Propagate agent metadata (token usage, messages) to Inspect.
            agent_meta = pop_agent_metadata(sample_id)
            if agent_meta.token_usage:
                state.metadata["agent_token_usage"] = agent_meta.token_usage
            if agent_meta.messages:
                state.metadata["agent_messages"] = agent_meta.messages

            return state

        return _run

    return _solver()


__all__ = [
    "Finding",
    "_domain_findings_to_eval",
    "_parse_github_repo",
    "openbot_review_solver",
]
