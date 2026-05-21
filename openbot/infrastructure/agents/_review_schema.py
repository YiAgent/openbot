"""Pydantic schemas for LLM structured output — slice B.

Why a separate module:

  - DeepAgents/LangGraph wants a *pydantic* ``response_format`` to coerce
    the agent's final answer into a typed object. We can't put pydantic
    in the domain layer (CLAUDE.md: pydantic only in ``openbot/core/settings.py``
    and infrastructure adapters), so the LLM-facing schema lives here and
    the responder converts to the domain ``ReviewFindings`` at the boundary.
  - Anti-corruption layer: the LLM might hand back severities we don't
    recognize, ``None`` where we expect a string, etc. ``to_domain`` is the
    single chokepoint that drops malformed entries — every consumer
    downstream gets a clean ``ReviewFindings``.

Field meanings match the domain types verbatim; see
``openbot/domain/review.py`` for the source of truth.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openbot.domain.review import Finding, ReviewFindings

_logger = logging.getLogger(__name__)

_VALID_SEVERITIES: frozenset[str] = frozenset(("critical", "high", "medium", "low", "nit"))


class _FindingModel(BaseModel):
    """One reviewer observation as the LLM may emit it.

    ``model_config`` forbids extras so the agent can't sneak free-form
    keys past us (e.g., a ``rule_id`` we don't yet support). Adding new
    fields is a deliberate code change.
    """

    model_config = ConfigDict(extra="forbid")

    severity: str = Field(description="critical | high | medium | low | nit")
    file: str = Field(description="Repo-relative path of the file with the issue.")
    message: str = Field(description="One- or two-sentence description of the issue.")
    line: int | None = Field(
        default=None,
        description=(
            "Line number in the file. Omit (or set null) for repo-wide findings "
            "(e.g., missing changelog) — these get folded into the review body."
        ),
    )
    quote: str | None = Field(
        default=None,
        description="Optional source snippet (≤ 200 chars) so the author can locate the issue.",
    )


class _ReviewFindingsModel(BaseModel):
    """Top-level reviewer output the agent fills in via ``response_format``."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="One-line top-of-review summary. Required even when findings is empty."
    )
    findings: list[_FindingModel] = Field(
        default_factory=list,
        description="Zero-or-more findings. Empty list → review approves with summary only.",
    )

    def to_domain(self) -> ReviewFindings:
        """Convert to the domain type, dropping unrecognized severities.

        We deliberately *drop* rather than *raise* — see
        ``passes_threshold``'s rationale. One bad finding shouldn't
        fail the whole review.
        """
        cleaned: list[Finding] = []
        for raw in self.findings:
            if raw.severity not in _VALID_SEVERITIES:
                _logger.info(
                    "review_finding_dropped_unknown_severity",
                    extra={"severity": raw.severity, "file": raw.file},
                )
                continue
            cleaned.append(
                Finding(
                    severity=raw.severity,  # type: ignore[arg-type]
                    file=raw.file,
                    message=raw.message,
                    line=raw.line,
                    quote=raw.quote,
                )
            )
        return ReviewFindings(summary=self.summary, findings=tuple(cleaned))


def parse_structured_response(raw: Any) -> ReviewFindings:
    """Coerce whatever the agent put in ``result["structured_response"]`` to domain.

    DeepAgents may return either a pydantic instance or a plain dict
    (depends on langchain version and model). Handle both, and fall
    through to an explicit error if neither shape is recognizable —
    silently approving on garbage input would be worse than failing
    loudly so the responder's outer ``except`` can post the error
    template.
    """
    if isinstance(raw, _ReviewFindingsModel):
        return raw.to_domain()
    if isinstance(raw, dict):
        return _ReviewFindingsModel.model_validate(raw).to_domain()
    if isinstance(raw, BaseModel):
        # Some other pydantic model — dict-roundtrip is the safest path.
        return _ReviewFindingsModel.model_validate(raw.model_dump()).to_domain()
    raise ValueError(f"deepagents_structured_response_unexpected_type:{type(raw).__name__}")


# Re-exported so callers don't need a second import.
ReviewFindingsSchema = _ReviewFindingsModel
FindingSchema = _FindingModel


__all__ = [
    "FindingSchema",
    "ReviewFindingsSchema",
    "parse_structured_response",
]
