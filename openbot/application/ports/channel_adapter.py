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
