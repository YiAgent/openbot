"""ChannelAdapter implementations.

PRD §13 #11: ABC stays day-1; v0.1 ships only GitHubAdapter. v0.2 adds
LinearAdapter; v0.3+ Slack / Discord.
"""

from openbot.infrastructure.adapters.base import ChannelAdapter, SignatureError
from openbot.infrastructure.adapters.github import GitHubAdapter

__all__ = ["ChannelAdapter", "GitHubAdapter", "SignatureError"]
