"""FakeConfigLoader — in-memory ConfigLoaderPort.

Returns ``config`` for every repo by default. Override per-repo via
``by_repo={repo: cfg}``. Records the repo full_name of every
``load_for_repo()`` call in ``loads``.

``config`` defaults to ``baked_in_defaults()`` so ``FakeConfigLoader()``
works with no args (required for the module-level ``_PROTOCOL_CHECK``).

Failure injection: ``fail_after=N`` raises ``fail_with(…)`` once the
call count reaches ``N`` (sticky).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from openbot.application.ports.config_loader import ConfigLoaderPort
from openbot.domain.config_schema import EffectiveConfig
from openbot.infrastructure.config_loader import baked_in_defaults

if TYPE_CHECKING:
    pass


@dataclass
class FakeConfigLoader:
    """In-memory ConfigLoaderPort. Construct fresh per test."""

    config: EffectiveConfig = field(default_factory=baked_in_defaults)
    by_repo: dict[str, EffectiveConfig] = field(default_factory=dict)
    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _loads: list[str] = field(default_factory=list, init=False)

    @property
    def loads(self) -> tuple[str, ...]:
        """All repo full_names passed to load_for_repo(), in order."""
        return tuple(self._loads)

    async def load_for_repo(self, adapter: Any, event: Any) -> EffectiveConfig:
        """See :class:`openbot.application.ports.config_loader.ConfigLoaderPort`."""
        self._maybe_fail()
        repo: str = getattr(event, "repo", "") or ""
        self._loads.append(repo)
        return self.by_repo.get(repo, self.config)

    def _maybe_fail(self) -> None:
        if self.fail_after is not None and len(self._loads) >= self.fail_after:
            raise self.fail_with("FakeConfigLoader: simulated failure")


# Static check: import-time error if FakeConfigLoader stops satisfying ConfigLoaderPort.
_PROTOCOL_CHECK: Final[ConfigLoaderPort] = FakeConfigLoader()


__all__ = ["FakeConfigLoader"]
