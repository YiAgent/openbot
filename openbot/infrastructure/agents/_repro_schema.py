"""Pydantic boundary for the reproduce responder — slice R1.

Why a separate module (same rationale as ``_fix_schema.py``):

  - DeepAgents/LangGraph wants a *pydantic* ``response_format`` to coerce
    the agent's final answer into a typed object. Pydantic must not
    cross into the domain layer (CLAUDE.md), so the LLM-facing schema
    lives here and ``parse_structured_response`` converts to
    ``ReproOutcome`` at the boundary.
  - Anti-corruption layer: ``model_config = ConfigDict(extra="forbid")``
    blocks free-form keys the agent might try to slip past us. Adding
    fields is a deliberate code change in both schema and domain.

Field meanings match ``openbot/domain/repro.py`` verbatim; that file is
the source of truth.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openbot.domain.repro import ReproOutcome, ReproStatus

# Same budget as ``ReproOutcome.output_excerpt`` is documented for —
# pinned by tests/domain/test_repro.py::test_output_excerpt_under_budget.
_OUTPUT_EXCERPT_BUDGET = 2000
_TRUNCATION_MARKER = "…[truncated]"


class _ReproOutcomeModel(BaseModel):
    """Reproduce outcome as the LLM may emit it.

    Mirrors ``ReproOutcome`` 1:1, except ``status`` is ``Literal[...]``
    so pydantic rejects unknown values at the boundary.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["reproduced", "not_reproduced", "insufficient_info", "agent_failed"] = Field(
        description="Terminal status of the reproduce attempt.",
    )
    summary: str = Field(description="1-2 sentence comment-friendly summary.")
    command: str | None = Field(
        default=None,
        description="Repro command actually executed; None for INSUFFICIENT_INFO.",
    )
    exit_code: int | None = Field(
        default=None,
        description="Exit code of ``command``; None when no command ran.",
    )
    output_excerpt: str = Field(
        default="",
        description=(
            "Truncated stdout+stderr from the repro run. The schema trims to 2000 chars + a marker."
        ),
    )
    hypothesis: str = Field(
        description=(
            "Agent's root-cause guess (reproduced / not_reproduced paths) "
            "or list of missing info (insufficient_info path)."
        ),
    )
    repro_artifact: str | None = Field(
        default=None,
        description=(
            "Git unified diff (output of git_diff()) of the test file written "
            "to reproduce the bug.  Used verbatim as SWT-bench model_patch. "
            "Paired with repro_artifact_filename; both None or both set."
        ),
    )
    repro_artifact_filename: str | None = Field(
        default=None,
        description="Path of the test file written (e.g. 'tests/test_repro_12907.py').",
    )

    @field_validator("output_excerpt", mode="before")
    @classmethod
    def _truncate_excerpt(cls, value: Any) -> Any:
        # The agent's prompt asks for a tail-truncated excerpt, but a
        # runaway prompt can still dump multi-KB blobs. The schema is
        # the cheapest place to enforce the audit-row budget.
        if isinstance(value, str) and len(value) > _OUTPUT_EXCERPT_BUDGET:
            keep = _OUTPUT_EXCERPT_BUDGET - len(_TRUNCATION_MARKER)
            return value[:keep] + _TRUNCATION_MARKER
        return value

    def to_domain(self) -> ReproOutcome:
        return ReproOutcome(
            status=ReproStatus(self.status),
            summary=self.summary,
            command=self.command,
            exit_code=self.exit_code,
            output_excerpt=self.output_excerpt,
            hypothesis=self.hypothesis,
            repro_artifact=self.repro_artifact,
            repro_artifact_filename=self.repro_artifact_filename,
        )


def parse_structured_response(raw: Any) -> ReproOutcome:
    """Coerce whatever the agent put in ``result["structured_response"]``
    to a domain ``ReproOutcome``.

    DeepAgents may return either a pydantic instance or a plain dict
    depending on langchain version. Both shapes are accepted; anything
    else raises ``ValueError`` so the responder's outer ``except`` posts
    the error template instead of silently constructing garbage data.
    """
    if isinstance(raw, _ReproOutcomeModel):
        return raw.to_domain()
    if isinstance(raw, dict):
        return _ReproOutcomeModel.model_validate(raw).to_domain()
    if isinstance(raw, BaseModel):
        return _ReproOutcomeModel.model_validate(raw.model_dump()).to_domain()
    raise ValueError(f"deepagents_repro_structured_response_unexpected_type:{type(raw).__name__}")


ReproOutcomeSchema = _ReproOutcomeModel


__all__ = [
    "ReproOutcomeSchema",
    "parse_structured_response",
]
