"""Core workflow enums — canonical home in the domain layer.

These three enums represent business semantics that belong to the domain:
  - Feature    — the four MVP workflow capabilities (LLM routing identity)
  - Workflow   — audit identity for workflow runs in audit_log
  - WorkflowPhase — lifecycle position for one workflow run

Both Feature and Workflow share the same four values (TRIAGE/REVIEW/FIX/CHAT)
but serve different purposes and are intentionally kept as separate enums.
They are in the domain layer because they are pure business concepts with no
I/O dependency.

Previously:
  Feature       lived in openbot.infrastructure.llm.model_router
  Workflow      lived in openbot.infrastructure.persistence.models
  WorkflowPhase lived in openbot.infrastructure.persistence.models

Infrastructure modules re-export these enums for backward compatibility.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Feature", "Workflow", "WorkflowPhase"]


class Feature(StrEnum):
    """The four MVP workflows. PRD §4 enumerates them.

    Used as the LLM routing identity — maps to per-feature model and
    budget configuration. See openbot.infrastructure.llm.model_router for
    the routing table.
    """

    TRIAGE = "triage"
    REVIEW = "review"
    FIX = "fix"
    CHAT = "chat"


class Workflow(StrEnum):
    """Workflow identity for ``audit_log.workflow`` and joined queries.

    Used as the audit identity — identifies which workflow produced an
    audit_log row. Distinct from Feature even though the values overlap,
    because Workflow is an audit/persistence concept while Feature is an
    LLM-routing concept.
    """

    TRIAGE = "triage"
    REVIEW = "review"
    FIX = "fix"
    CHAT = "chat"


class WorkflowPhase(StrEnum):
    """Lifecycle position for one workflow run.

    Written to ``audit_log.phase`` at each transition:
    STARTED → COMPLETED | FAILED | SKIPPED | REJECTED
    """

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"  # signature mismatch / unauthorized
