"""Pure value objects for PR review findings — slice B.

Why this lives in the domain layer (not infrastructure):

  - The use case (`maybe_run_review`) filters findings by severity and
    decides ``APPROVE`` vs ``COMMENT`` before any adapter call. That logic
    must not depend on pydantic, langchain, or HTTP shapes.
  - The responder (`DeepAgentsReviewResponder`) returns this type, not the
    pydantic schema it uses to coerce LLM output. Pydantic is an
    *infrastructure* concern — it stops at the boundary in
    ``openbot/infrastructure/agents/_review_schema.py``.

Severity ranking (most → least severe):

    critical > high > medium > low > nit

``passes_threshold`` is intentionally lenient about unknown severities:
if the LLM invents one ("catastrophic", "blocker"), we drop the finding
rather than crash the review. Surprising-but-correct: we'd rather lose a
finding than fail the whole run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from openbot.domain.config_schema import SeverityThreshold

Severity = Literal["critical", "high", "medium", "low", "nit"]

# Lower index = more severe. ``SeverityThreshold`` (from config) is a
# strict subset that omits ``nit`` — a threshold of "nit" would be useless
# (it'd keep every nitpick).
_SEVERITY_ORDER: tuple[Severity, ...] = ("critical", "high", "medium", "low", "nit")


@dataclass(frozen=True, slots=True)
class Finding:
    """One reviewer observation.

    ``line`` is ``None`` for repo-wide findings ("missing CHANGELOG entry");
    those get folded into the review body since GitHub's PR Review API
    requires a line number for inline comments.

    ``quote`` is an optional snippet of the offending source (≤ 200 chars
    by convention); useful for the author to locate the issue without
    clicking through to the file.
    """

    severity: Severity
    file: str
    message: str
    line: int | None = None
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewFindings:
    """The full reviewer output — a summary plus zero-or-more findings.

    Empty ``findings`` → the use case posts an APPROVE review with just
    the summary. Non-empty → COMMENT review with inline comments per
    finding (per-finding ``line is None`` get folded into the body).
    """

    summary: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)


def passes_threshold(finding: Finding, threshold: SeverityThreshold) -> bool:
    """Return True if ``finding`` is at least as severe as ``threshold``.

    Examples (threshold=medium): critical/high/medium → True; low/nit → False.

    Unknown severities (LLM hallucination) → False. We'd rather silently
    drop a finding than crash the whole review run.
    """
    try:
        finding_rank = _SEVERITY_ORDER.index(finding.severity)
    except ValueError:
        return False
    threshold_rank = _SEVERITY_ORDER.index(threshold)
    return finding_rank <= threshold_rank


__all__ = [
    "Finding",
    "ReviewFindings",
    "Severity",
    "passes_threshold",
]
