"""UnifiedEvent — channel-agnostic event shape.

PRD §5.1 / §13 #11: ChannelAdapter ABC normalizes GitHub / Linear / Slack
webhooks into a single shape so the Router and workflows stay channel-agnostic.

Anything reaching downstream code MUST come through this type — never pass raw
webhook payloads to workflows or LLM prompts (PRD §4.8 prompt-injection defense).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    """The subset of channel events OpenBot v0.1 reacts to.

    PRD §4 triggers:
      - triage  → ISSUE_OPENED, ISSUE_EDITED, ISSUE_REOPENED
      - review  → PR_OPENED, PR_SYNCHRONIZED, PR_REOPENED
      - fix     → ISSUE_ASSIGNED (assignee includes the bot)
      - chat    → ISSUE_COMMENT_CREATED, PR_REVIEW_COMMENT_CREATED
      - cancel  → ISSUE_CLOSED, PR_CLOSED, PR_MERGED
    """

    ISSUE_OPENED = "issue.opened"
    ISSUE_EDITED = "issue.edited"
    ISSUE_ASSIGNED = "issue.assigned"
    ISSUE_COMMENT_CREATED = "issue_comment.created"
    ISSUE_CLOSED = "issue.closed"
    ISSUE_REOPENED = "issue.reopened"
    PR_OPENED = "pull_request.opened"
    PR_SYNCHRONIZED = "pull_request.synchronize"
    PR_CLOSED = "pull_request.closed"
    PR_REOPENED = "pull_request.reopened"
    PR_MERGED = "pull_request.merged"
    PR_REVIEW_COMMENT_CREATED = "pull_request_review_comment.created"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class UnifiedEvent:
    """Normalized event from any ChannelAdapter.

    `raw` holds the original payload for downstream tools that need details
    (e.g. PR review needs the diff URL). Treat `raw` as untrusted user content —
    never embed unsanitized into LLM prompts.
    """

    channel: str
    delivery_id: str
    kind: EventKind
    repo: str
    actor: str
    issue_number: int | None = None
    pr_number: int | None = None
    comment_body: str | None = None
    # GitHub `sender.type` — typically "User" or "Bot" (also "Organization" rarely).
    # Workflows that respond to comments / PRs MUST gate on `not is_from_bot` to
    # avoid echo loops: a bot's own reply fires another comment.created webhook,
    # which would re-enter the same workflow indefinitely if unfiltered.
    actor_type: str | None = None
    # Channel-specific token scope. For GitHub, the App installation id —
    # required to mint an installation token before any write-back API call.
    # Present in every authentic GitHub webhook payload under `installation.id`.
    installation_id: int | None = None
    # Monotonic per-resource sequence — for GitHub we derive this from
    # ``updated_at`` (epoch ms) of ``issue`` / ``pull_request`` so late
    # webhooks (close-arrives-before-open after a retry) can be detected
    # by the state machine and dropped as ``IGNORE``. ``0`` is the safe
    # sentinel (no ``updated_at`` available); the classifier treats 0 as
    # "never older than the high-water mark" so we never spuriously drop.
    event_seq: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_relevant(self) -> bool:
        """Whether the Router should dispatch a workflow."""
        return self.kind is not EventKind.UNKNOWN

    @property
    def is_from_bot(self) -> bool:
        """True iff the event was authored by a GitHub App bot identity.

        Resolves True for `openbot-dev[bot]`, `dependabot[bot]`,
        `github-actions[bot]`, etc.; False for any human account regardless of
        login suffix. The check is on `actor_type`, not login pattern — login
        suffixes are not part of GitHub's API contract; `sender.type` is.
        """
        return self.actor_type == "Bot"

    @property
    def resource_key(self) -> str | None:
        """Per-resource identifier for the state machine.

        Shape: ``github:{owner/repo}:issue:{n}`` or ``github:{owner/repo}:pr:{n}``.

        Returns ``None`` when no issue/PR number is present (e.g. ``ping``,
        ``push``) — the caller should treat that as "not stateful".
        """
        if self.pr_number is not None:
            return f"{self.channel}:{self.repo}:pr:{self.pr_number}"
        if self.issue_number is not None:
            return f"{self.channel}:{self.repo}:issue:{self.issue_number}"
        return None
