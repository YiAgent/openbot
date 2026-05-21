"""Pydantic schemas for fix responder structured output — slice C.

Why a separate module (same rationale as ``_review_schema.py``):

  - DeepAgents/LangGraph wants a *pydantic* ``response_format`` to coerce
    the agent's final answer into a typed object. Pydantic must not
    cross into the domain layer (CLAUDE.md), so the LLM-facing schema
    lives here and the responder converts to ``FixOutcome`` at the
    boundary.
  - Anti-corruption layer: ``parse_structured_response`` is the single
    chokepoint between LLM output and the use case. Bad payloads fail
    loudly so the responder's outer ``except`` posts the error template
    instead of silently constructing garbage data.

Field meanings match ``openbot/domain/fix.py`` verbatim; that file is
the source of truth.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openbot.domain.fix import FixAttempt, FixOutcome


class _FixAttemptModel(BaseModel):
    """One reasoning pass as the LLM may emit it.

    ``model_config`` forbids extras so the agent can't sneak free-form
    keys past us (e.g., a ``confidence`` we don't yet support). Adding
    fields is a deliberate code change in both schema and domain.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="One-line description of the change.")
    files_changed: list[str] = Field(
        default_factory=list,
        description="Repo-relative paths the agent wrote.",
    )
    tests_passed: bool = Field(description="True iff the final test run exited 0.")
    test_command: str = Field(description="The exact shell command the agent ran for tests.")
    test_output: str = Field(
        description="Truncated stdout+stderr from the test run.",
    )
    diff: str = Field(
        default="",
        description="git diff of the working tree after edits.",
    )

    def to_domain(self) -> FixAttempt:
        return FixAttempt(
            summary=self.summary,
            files_changed=tuple(self.files_changed),
            tests_passed=self.tests_passed,
            test_command=self.test_command,
            test_output=self.test_output,
            diff=self.diff,
        )


class _FixOutcomeModel(BaseModel):
    """Top-level fix-loop output the agent fills via ``response_format``.

    ``pr_url`` and ``error`` are unused on the LLM side — the use case
    sets them after the agent returns (when it opens the PR, or when a
    downstream step raises). We keep them in the schema so the same
    pydantic model round-trips through tests.
    """

    model_config = ConfigDict(extra="forbid")

    attempt: _FixAttemptModel
    pr_url: str | None = Field(default=None)
    error: str | None = Field(default=None)

    def to_domain(self) -> FixOutcome:
        return FixOutcome(
            attempt=self.attempt.to_domain(),
            pr_url=self.pr_url,
            error=self.error,
        )


def parse_structured_response(raw: Any) -> FixOutcome:
    """Coerce whatever the agent put in ``result["structured_response"]``
    to a domain ``FixOutcome``.

    DeepAgents may return either a pydantic instance or a plain dict
    depending on langchain version. Both shapes are accepted; anything
    else raises so the responder's outer ``except`` posts the error
    template instead of silently constructing garbage data.
    """
    if isinstance(raw, _FixOutcomeModel):
        return raw.to_domain()
    if isinstance(raw, dict):
        return _FixOutcomeModel.model_validate(raw).to_domain()
    if isinstance(raw, BaseModel):
        return _FixOutcomeModel.model_validate(raw.model_dump()).to_domain()
    raise ValueError(f"deepagents_fix_structured_response_unexpected_type:{type(raw).__name__}")


FixOutcomeSchema = _FixOutcomeModel
FixAttemptSchema = _FixAttemptModel


__all__ = [
    "FixAttemptSchema",
    "FixOutcomeSchema",
    "parse_structured_response",
]
