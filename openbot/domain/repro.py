"""Pure value objects for the reproduce stage of triage — slice R1.

Why this lives in the domain layer (mirrors ``openbot/domain/fix.py``):

  - The use case (``maybe_run_triage``) decides which comment template
    to render based on ``outcome.status`` without touching pydantic,
    langchain, or HTTP shapes.
  - The responder (``DeepAgentsReproResponder``) returns these types,
    not the pydantic schema used to coerce LLM output. Pydantic stops
    at ``openbot/infrastructure/agents/_repro_schema.py``.

Invariants are asserted in tests (not in ``__post_init__``) because
partial outcomes do exist — e.g. ``AGENT_FAILED`` after a command ran
but before ``exit_code`` was captured must still be representable as
``ReproOutcome(status=AGENT_FAILED, command="...", exit_code=None, ...)``.
Constructor-side enforcement would erase the partial state we most
want to record in the audit row.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReproStatus(StrEnum):
    """Terminal status of a reproduce attempt.

    The string values land in the audit log and Langfuse metadata.
    Changing them is a wire-format break — pinned by
    ``tests/domain/test_repro.py::test_repro_status_values_are_stable``.
    """

    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    INSUFFICIENT_INFO = "insufficient_info"
    AGENT_FAILED = "agent_failed"


@dataclass(frozen=True, slots=True)
class ReproOutcome:
    """End-to-end result of one reproduce run.

    ``command`` / ``exit_code`` are populated when the agent actually ran
    a shell command (REPRODUCED, NOT_REPRODUCED, and AGENT_FAILED-after-run
    paths). INSUFFICIENT_INFO outcomes leave them ``None``.

    ``repro_artifact`` is the agent's verbatim repro script body — a
    string, kept inline in the audit row (capped at the same 2000-char
    budget as ``output_excerpt`` via the responder). v0.2 fix-handoff
    will promote this to an on-disk script; until then inline keeps the
    audit row self-contained.

    ``repro_artifact_filename`` is the suggested filename for that v0.2
    promotion (e.g. ``"repro_reproduced.sh"``). Both fields are paired:
    both ``None`` or both set — pinned by tests, not ``__post_init__``.
    """

    status: ReproStatus
    summary: str
    command: str | None
    exit_code: int | None
    output_excerpt: str
    hypothesis: str
    repro_artifact: str | None
    repro_artifact_filename: str | None


__all__ = ["ReproOutcome", "ReproStatus"]
