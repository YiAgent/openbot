"""ChannelAdapterPort — channel-agnostic interaction surface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from openbot.domain.events import UnifiedEvent


@runtime_checkable
class ChannelAdapterPort(Protocol):
    """One per channel. Currently only GitHub is implemented."""

    name: str

    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> None:
        """Raise SignatureError on auth failure."""
        ...

    def parse_event(self, body: bytes, headers: Mapping[str, str]) -> UnifiedEvent:
        """Decode the authenticated payload."""
        ...

    async def reply(self, event: UnifiedEvent, message: str) -> dict[str, Any]:
        """Post a reply on the originating thread."""
        ...

    async def get_issue_labels(self, event: UnifiedEvent, number: int) -> frozenset[str]:
        """Return the set of label names on the given issue/PR number.

        Returns an empty frozenset on failure.
        """
        ...

    async def get_pr_comments(self, event: UnifiedEvent, pr_number: int) -> list[dict[str, Any]]:
        """Return PR comments (up to 100). Returns [] on failure."""
        ...

    async def get_actor_role(self, event: UnifiedEvent, login: str | None = None) -> str:
        """Return the repo permission role of `login` (or event.actor if None).

        One of: 'admin' | 'maintain' | 'write' | 'triage' | 'read' | 'none'.
        Returns 'none' on failure.
        """
        ...

    async def update_check_run(
        self,
        event: UnifiedEvent,
        check_run_id: int,
        status: str = "completed",
        conclusion: str | None = None,
        completed_at: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update an existing check run's status and output.

        Args:
            event: The unified event context.
            check_run_id: The numeric ID of the check run to update.
            status: The new status (e.g., 'completed').
            conclusion: The conclusion (e.g., 'success', 'failure', 'skipped').
            completed_at: Timestamp when the check run completed.
            output: Output dict with 'title' and 'summary' fields.

        Returns:
            The updated check run object.
        """
        ...

    async def fetch_repo_file(
        self,
        event: UnifiedEvent,
        path: str,
    ) -> bytes | None:
        """Fetch a file from the repo at `path`. Returns None if not found (404).

        Raises on other HTTP errors (auth failures, server errors).
        """
        ...

    async def add_label(self, event: UnifiedEvent, *labels: str) -> list[dict[str, Any]]:
        """Add one or more labels to the issue or PR referenced by *event*.

        Returns a list of created label objects (same shape as GitHub API response).
        Implementations may return [] on a no-op or if labels already exist.
        """
        ...
