"""Structured-output schemas for eval solvers.

Each eval cell binds the corresponding schema to ``deepagents``'
``response_format=`` so the agent's terminal step *must* produce the
exact shape the downstream scoring layer expects. The schemas are also
the on-the-wire format when exporting predictions for the official
benchmark harnesses (SWE-bench / SWT-bench Docker), so the field names
match those harnesses verbatim — do not rename without updating the
exporter and the upstream harness flags.

Three schemas live here:

- :class:`SweBenchPrediction` — SWE-bench Verified (``model_patch`` is a
  unified diff that touches *production* code).
- :class:`SwtBenchPrediction` — SWT-Bench Verified. SWT-Bench's official
  ``run_evaluation.py`` consumes the **same** record shape as SWE-bench —
  the ``model_patch`` field contains a test-only diff.
- :class:`SweQaProAnswer` — SWE-QA-Pro free-form answer plus structured
  evidence citations (mirrors Appendix D ``<finish>`` block content).

Convenience: :func:`empty_swe_prediction` builds a no-op prediction when
the agent failed to make any edits — keeps the downstream JSONL valid
rather than skipping the row (which would be silently treated as 0/N
by the official harness in either case, but the explicit empty patch
makes the failure visible in the leaderboard CSV).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    """Forbid extra keys so schema drift fails loudly at parse time."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SweBenchPrediction(_StrictModel):
    """Single row of an SWE-bench ``predictions.jsonl``.

    Field names follow the official harness:
    https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/run_evaluation.py
    """

    instance_id: str = Field(description="Dataset row id, e.g. astropy__astropy-12907.")
    model_name_or_path: str = Field(
        description="Free-form identifier of the model + scaffold combo. "
        "Used for naming the result directory in the upstream harness.",
    )
    model_patch: str = Field(
        description="Unified diff of the agent's edits vs the base commit. "
        "Empty string means no edits — kept for completeness so the row "
        "still validates."
    )


class SwtBenchPrediction(_StrictModel):
    """Single row of an SWT-Bench ``predictions.jsonl``.

    SWT-Bench shares the SWE-bench input schema; the ``model_patch`` field
    is expected to touch *only* test files (the upstream grader rejects
    non-test edits). Kept as a distinct class so we can extend it later
    without breaking SWE-bench callers.
    """

    instance_id: str
    model_name_or_path: str
    model_patch: str


class SweQaProCitation(_StrictModel):
    """Single evidence citation in an SWE-QA-Pro answer.

    Lines are 1-indexed, inclusive. ``relative_path`` is relative to the
    repo root inside ``/workspace`` so it matches the paper's grading
    convention (Appendix D "<relative_path>: line <start>-<end>").
    """

    relative_path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class SweQaProAnswer(_StrictModel):
    """Final answer + structured citations for one SWE-QA-Pro question.

    The paper's grader scores only the answer body; the structured citations
    are for our own offline analysis (citation-rate is a useful quality
    signal even though it doesn't enter the public score).
    """

    answer: str = Field(description="The natural-language final answer.")
    citations: list[SweQaProCitation] = Field(default_factory=list)


def empty_swe_prediction(
    *,
    instance_id: str,
    model_name_or_path: str,
) -> SweBenchPrediction:
    """Build a no-edit SWE-bench prediction row.

    Used when the agent errored out or produced no diff — keeps the
    ``predictions.jsonl`` round-trippable so the leaderboard still shows
    the instance as attempted-and-failed instead of silently absent.
    """
    return SweBenchPrediction(
        instance_id=instance_id,
        model_name_or_path=model_name_or_path,
        model_patch="",
    )


def empty_swt_prediction(
    *,
    instance_id: str,
    model_name_or_path: str,
) -> SwtBenchPrediction:
    """SWT-Bench counterpart to :func:`empty_swe_prediction`."""
    return SwtBenchPrediction(
        instance_id=instance_id,
        model_name_or_path=model_name_or_path,
        model_patch="",
    )
