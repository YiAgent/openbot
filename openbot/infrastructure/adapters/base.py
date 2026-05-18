"""ChannelAdapter ABC — abstract base for GitHub / Linear / Slack ingest.

PRD §13 #11: declared on day 1 even though v0.1 only implements GitHubAdapter,
so v0.2 LinearAdapter and v0.3 Slack/Discord slot in without touching the
Router or workflows.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from openbot.domain.events import UnifiedEvent


class SignatureError(Exception):
    """Webhook authenticity check failed — maps to HTTP 401."""


class ChannelAdapter(ABC):
    """One per channel. Stateless aside from secrets injected at construction.

    Concrete adapters MUST use `hmac.compare_digest` for any secret-derived
    MAC comparison to avoid timing side-channels (PRD §4.8).
    """

    name: str  # "github" | "linear" | "slack" | ...

    @abstractmethod
    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> None:
        """Raise `SignatureError` if the payload isn't authentic.

        The caller MUST pass the RAW request body bytes — JSON-parsing before
        verification would let an attacker bypass the MAC via canonicalization
        differences.
        """

    @abstractmethod
    def parse_event(self, body: bytes, headers: Mapping[str, str]) -> UnifiedEvent:
        """Convert authenticated payload → channel-agnostic UnifiedEvent.

        The caller MUST have invoked `verify_signature` first. Implementations
        return `EventKind.UNKNOWN` for shapes we don't react to yet (e.g.
        push, star) — never raise on unknown event types.
        """
