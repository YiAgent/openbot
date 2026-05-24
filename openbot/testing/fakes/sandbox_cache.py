"""FakeSandboxCache — in-memory SandboxCachePort.

``acquire`` returns ``None`` (cache miss) by default. Inject specific
returns via ``responses=[handle, None, …]`` — each call pops the front.

Every call is recorded as a ``CacheOp`` frozen dataclass in ``ops``.
``op_type`` discriminates between ``"acquire"``, ``"publish"``, and
``"evict_repo"``.

Failure injection: ``fail_after=N`` raises ``fail_with(…)`` once the
combined call count reaches ``N`` (sticky).

All-defaults constructor: ``FakeSandboxCache()`` works with no args.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from openbot.application.ports.sandbox_cache import SandboxCachePort

if TYPE_CHECKING:
    from openbot.application.sandbox_handle import SandboxedHandle
    from openbot.domain.checkout import CheckoutSpec


@dataclass(frozen=True)
class CacheOp:
    """Snapshot of one acquire/publish/evict_repo call. All fields immutable."""

    op_type: str  # "acquire" | "publish" | "evict_repo"
    installation_id: int
    repo_url: str | None = None  # populated for evict_repo calls


@dataclass
class FakeSandboxCache:
    """In-memory SandboxCachePort. Construct fresh per test."""

    responses: list[SandboxedHandle | None] = field(default_factory=list)
    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _ops: list[CacheOp] = field(default_factory=list, init=False)

    @property
    def ops(self) -> tuple[CacheOp, ...]:
        """All cache operations in order, as an immutable tuple."""
        return tuple(self._ops)

    async def acquire(
        self,
        checkout: CheckoutSpec,
        token: str,
        *,
        installation_id: int,
    ) -> SandboxedHandle | None:
        """See :class:`openbot.application.ports.sandbox_cache.SandboxCachePort`."""
        self._maybe_fail()
        self._ops.append(CacheOp(op_type="acquire", installation_id=installation_id))
        return self.responses.pop(0) if self.responses else None

    async def publish(
        self,
        handle: SandboxedHandle,
        *,
        installation_id: int,
    ) -> None:
        """See :class:`openbot.application.ports.sandbox_cache.SandboxCachePort`."""
        self._maybe_fail()
        self._ops.append(CacheOp(op_type="publish", installation_id=installation_id))

    async def evict_repo(self, repo_url: str, *, installation_id: int) -> None:
        """See :class:`openbot.application.ports.sandbox_cache.SandboxCachePort`."""
        self._maybe_fail()
        self._ops.append(
            CacheOp(op_type="evict_repo", installation_id=installation_id, repo_url=repo_url)
        )

    def _maybe_fail(self) -> None:
        if self.fail_after is not None and len(self._ops) >= self.fail_after:
            raise self.fail_with("FakeSandboxCache: simulated failure")


# Static check: import-time error if FakeSandboxCache stops satisfying SandboxCachePort.
_PROTOCOL_CHECK: Final[SandboxCachePort] = FakeSandboxCache()


__all__ = ["CacheOp", "FakeSandboxCache"]
