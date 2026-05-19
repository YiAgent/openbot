"""ConfigLoaderPort — per-repo effective config resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.domain.config_schema import EffectiveConfig
    from openbot.domain.events import UnifiedEvent


class ConfigLoaderPort(Protocol):
    """Resolve the effective config for one repo.

    Both `adapter` and `event` are required at call time because the
    YAML loader fetches from the channel's contents API using the
    installation token derived from `event`.
    """

    async def load_for_repo(
        self,
        adapter: ChannelAdapterPort,
        event: UnifiedEvent,
    ) -> EffectiveConfig:
        """Return the merged effective config — defaults + repo overrides."""
        ...


__all__ = ["ConfigLoaderPort"]
