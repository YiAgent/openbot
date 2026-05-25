"""FakeResourceLock — in-memory ResourceLockPort.

``lock()`` is a sync method returning an async context manager that
yields ``True`` (acquired) by default. Add a resource_key to
``contended_keys`` to make that key yield ``False`` (contended).

Observation: ``acquired_keys`` records every key where the CM yielded
``True``; contended keys are not recorded there.

Failure injection: ``fail_after=N`` raises ``fail_with(…)`` once the
acquire count reaches ``N`` (sticky, counts successful acquires only).

All-defaults constructor: ``FakeResourceLock()`` works with no args.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from openbot.application.ports.resource_lock import ResourceLockPort


@dataclass(frozen=True)
class LockRecord:
    """Snapshot of one lock() acquisition. All fields immutable."""

    resource_key: str
    ttl_seconds: int
    acquired: bool


@dataclass
class FakeResourceLock:
    """In-memory ResourceLockPort. Construct fresh per test."""

    contended_keys: set[str] = field(default_factory=set)
    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _lock_records: list[LockRecord] = field(default_factory=list, init=False)

    @property
    def acquired_keys(self) -> tuple[str, ...]:
        """All successfully acquired resource_keys in order."""
        return tuple(r.resource_key for r in self._lock_records if r.acquired)

    @property
    def lock_records(self) -> tuple[LockRecord, ...]:
        """All lock() attempts (acquired + contended) in order."""
        return tuple(self._lock_records)

    def lock(
        self, resource_key: str, *, ttl_seconds: int = 10
    ) -> AbstractAsyncContextManager[bool]:
        """See :class:`openbot.application.ports.resource_lock.ResourceLockPort`."""

        @asynccontextmanager
        async def _cm():  # type: ignore[return]
            acquired = resource_key not in self.contended_keys
            if acquired and self.fail_after is not None:
                count = sum(1 for r in self._lock_records if r.acquired)
                if count >= self.fail_after:
                    raise self.fail_with("FakeResourceLock: simulated failure")
            self._lock_records.append(
                LockRecord(resource_key=resource_key, ttl_seconds=ttl_seconds, acquired=acquired)
            )
            yield acquired

        return _cm()


# Static check: import-time error if FakeResourceLock stops satisfying ResourceLockPort.
_PROTOCOL_CHECK: Final[ResourceLockPort] = FakeResourceLock()


__all__ = ["FakeResourceLock", "LockRecord"]
