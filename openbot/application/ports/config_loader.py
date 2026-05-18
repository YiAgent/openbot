"""ConfigLoaderPort — per-repo effective config resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from openbot.domain.config_schema import EffectiveConfig
    from openbot.domain.events import UnifiedEvent


class ConfigLoaderPort(Protocol):
    """Resolve the effective config for one repo.

    Both `adapter` and `event` are required at call time because the
    YAML loader fetches from the GitHub Contents API using the
    installation token derived from `event`.
    """

    async def load_for_repo(
        self,
        adapter: Any,
        event: UnifiedEvent,
    ) -> EffectiveConfig:
        """Return the merged effective config — defaults + repo overrides."""
        ...


__all__ = ["ConfigLoaderPort"]
