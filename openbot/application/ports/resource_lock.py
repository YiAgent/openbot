"""ResourceLockPort — per-resource_key mutual exclusion."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol


class ResourceLockPort(Protocol):
    """Acquire and hold a per-resource lock for the duration of a `with`."""

    def lock(
        self, resource_key: str, *, ttl_seconds: int = 10
    ) -> AbstractAsyncContextManager[bool]:
        """Async-CM yielding True if acquired (or fallback-open), False on contention."""
        ...
