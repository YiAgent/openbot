"""FakeConfigLoader — returns programmable EffectiveConfig per repo."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openbot.domain.config_schema import EffectiveConfig
from openbot.infrastructure.config_loader import baked_in_defaults


@dataclass
class FakeConfigLoader:
    """Deterministic config loader for tests.

    Look up by ``event.repo``; fall back to ``default`` if provided.
    """

    by_repo: dict[str, EffectiveConfig] = field(default_factory=dict)
    default: EffectiveConfig | None = None
    calls: list[str] = field(default_factory=list)

    async def load_for_repo(self, adapter: Any, event: Any) -> EffectiveConfig:
        repo = event.repo or ""
        self.calls.append(repo)
        if repo in self.by_repo:
            return self.by_repo[repo]
        if self.default is not None:
            return self.default
        return baked_in_defaults()


__all__ = ["FakeConfigLoader"]
