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
      - triage  → ISSUE_OPENED
      - review  → PR_OPENED, PR_SYNCHRONIZED
      - fix     → ISSUE_ASSIGNED (assignee includes the bot)
      - chat    → ISSUE_COMMENT_CREATED, PR_REVIEW_COMMENT_CREATED
    """

    ISSUE_OPENED = "issue.opened"
    ISSUE_ASSIGNED = "issue.assigned"
    ISSUE_COMMENT_CREATED = "issue_comment.created"
    PR_OPENED = "pull_request.opened"
    PR_SYNCHRONIZED = "pull_request.synchronize"
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
