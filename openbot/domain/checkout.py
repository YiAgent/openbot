"""Pure value objects for the unified sandbox-entry checkout step.

Why this lives in ``domain/`` (mirrors ``domain/fix.py`` / ``domain/review.py``):

  - ``CheckoutSpec`` is the contract between the resolver (application
    layer) and the dispatcher's clone step. Nothing in here imports
    pydantic, langchain, HTTP, or GitHub-specific payloads.
  - ``CloneStrategy`` is exposed all the way to the sandbox port so
    adapters (``DaytonaSandboxAdapter``, ``FakeSandboxAdapter``) can
    translate the intent into their own command shape.

Invariants:

  - ``ref`` MUST be a concrete SHA, not a branch name. Branches are
    mutable and can change between webhook receipt and clone — passing a
    branch here would silently race.
  - ``diff_base`` is non-None only for the REVIEW workflow; other
    handlers ignore it.
  - ``sparse_paths`` is reserved for future monorepo support; today the
    default empty tuple means "no sparse-checkout".

See ``docs/superpowers/specs/2026-05-21-unified-sandbox-entry-design.md``
§ "Data model" for the full design rationale and § "The ref-resolution
matrix" for the (event_kind, workflow) -> CheckoutSpec mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class CloneStrategy(StrEnum):
    """How much git materializes on clone.

    Ordered roughly by cost (cheap → expensive):

      - ``BLOBLESS`` (``--filter=blob:none``): metadata only, lazy blob
        fetch on read. Fast for "list files + read a few" patterns —
        chat / inline-review-comment workflows.
      - ``SHALLOW`` (``--depth=1``): one commit, no history. The
        default; covers triage repro and fix.
      - ``SHALLOW_HISTORY`` (``--depth=N``): enough history for
        incremental diff against an earlier base. Review uses this.
      - ``FULL``: full clone. Reserved for workflows that need
        ``git blame`` / ``git log`` heavily — none in v0.1.

    Adapters MAY ignore the strategy (``FakeSandboxAdapter`` uses a
    local tmpdir and treats every value as equivalent); the production
    Daytona adapter switches the git command shape per value.
    """

    BLOBLESS = "blobless"
    SHALLOW = "shallow"
    SHALLOW_HISTORY = "shallow_history"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class CheckoutSpec:
    """The exact (repo, ref, strategy) any workflow needs in its sandbox.

    Constructed by ``resolve_checkout`` (application layer); consumed by
    the dispatcher's clone step. Pure value type — no I/O, no side
    effects.

    ``diff_base`` is set only for the REVIEW workflow, where the
    handler needs to compute a diff against a different SHA than the
    checked-out HEAD. ``sparse_paths`` is reserved for monorepo support
    and unused in v0.1.
    """

    repo_url: str
    ref: str
    strategy: CloneStrategy = CloneStrategy.SHALLOW
    diff_base: str | None = None
    sparse_paths: tuple[str, ...] = field(default_factory=tuple)


class CheckoutResolutionError(Exception):
    """No resolution rule matches the (event_kind, workflow) pair.

    The dispatcher catches this and falls into the graceful-degrade
    path: handler is still called, but with ``sandbox_handle is None``,
    and the handler chooses a workflow-specific fallback reply.
    """


__all__ = [
    "CheckoutResolutionError",
    "CheckoutSpec",
    "CloneStrategy",
]
