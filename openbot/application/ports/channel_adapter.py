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
