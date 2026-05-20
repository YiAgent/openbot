# openbot/dispatcher/context.py
"""D11: Pure event-context extraction for direct-action rule evaluation.

Converts ``UnifiedEvent.raw`` (untyped dict) into a typed ``EventContext``
so downstream rule functions work with structured data and no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openbot.domain.events import UnifiedEvent

__all__ = ["EventContext", "extract_event_context"]


@dataclass(frozen=True, slots=True)
class EventContext:
    """Structured fields extracted from a GitHub webhook payload.

    ``issue_body``:
      - ``None``  — the "body" key was absent OR the value was null in JSON
      - ``""``    — the body key was present and explicitly an empty string
      - otherwise — the actual body string

    ``pr_additions``, ``pr_deletions``, ``pr_changed_files``:
      Zero when not a PR event or when the payload omits these fields.
    """

    issue_body: str | None
    issue_title: str | None
    issue_labels: tuple[str, ...]
    pr_additions: int
    pr_deletions: int
    pr_changed_files: int
    mention_body: str | None

    @property
    def pr_total_lines_changed(self) -> int:
        """Sum of additions + deletions (lines touched, not net diff)."""
        return self.pr_additions + self.pr_deletions


def extract_event_context(event: UnifiedEvent) -> EventContext:
    """Extract structured context from *event* without performing any I/O."""
    raw: dict[str, Any] = event.raw or {}
    issue: dict[str, Any] = raw.get("issue") or {}
    pr: dict[str, Any] = raw.get("pull_request") or {}

    # Distinguish absent key from JSON null / empty string.
    if "body" not in issue:
        issue_body: str | None = None
    else:
        raw_body = issue["body"]
        issue_body = str(raw_body) if raw_body is not None else None

    issue_labels: list[str] = []
    for lbl in issue.get("labels") or []:
        if isinstance(lbl, dict) and lbl.get("name"):
            issue_labels.append(str(lbl["name"]))

    return EventContext(
        issue_body=issue_body,
        issue_title=issue.get("title"),
        issue_labels=tuple(issue_labels),
        pr_additions=int(pr.get("additions") or 0),
        pr_deletions=int(pr.get("deletions") or 0),
        pr_changed_files=int(pr.get("changed_files") or 0),
        mention_body=event.comment_body,
    )
