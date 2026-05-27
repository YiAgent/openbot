"""Composition-root factory for the per-event sandbox.

Returns a *factory callable* — a zero-argument function that, when
called, produces an ``AsyncContextManager[SandboxPort]``. The caller
(``_execute_task_spec`` in the worker, and ``execute_handler`` in the
dispatcher) is responsible for calling the factory once per event and
closing the context when the handler returns.

Design mirrors ``sandbox_cache_deps.build_sandbox_cache``:

  - ``daytona_api_key`` absent  → return ``None``  (fix loop degrades;
    events that require a sandbox are rejected with an informative log
    instead of raising at import time).
  - ``daytona_api_key`` present → return an ``asynccontextmanager``
    factory that provisions a fresh ``DaytonaSandboxAdapter`` on enter
    and calls ``adapter.close()`` on exit (even on exceptions).

The ``daytona`` package is imported lazily inside the factory so a
deployment without ``daytona`` installed can import this module freely.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbot.application.ports.sandbox import SandboxPort
    from openbot.core.settings import Settings

_logger = logging.getLogger(__name__)

# Global semaphore — caps concurrent sandbox creation across all eval solvers
# so parallel Inspect samples don't blow past the Daytona org CPU limit.
# 2 is conservative: each sandbox uses ~2-4 CPUs; the org limit is 10.
_SANDBOX_SEM: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _SANDBOX_SEM
    if _SANDBOX_SEM is None:
        _SANDBOX_SEM = asyncio.Semaphore(2)
    return _SANDBOX_SEM


def build_sandbox_factory(
    settings: Settings,
) -> Callable[[], AbstractAsyncContextManager[SandboxPort]] | None:
    """Return a per-event sandbox factory, or ``None`` if unconfigured.

    Args:
        settings: The effective ``Settings`` instance.

    Returns:
        A zero-argument callable whose return value is an async context
        manager that yields a fresh ``DaytonaSandboxAdapter`` on enter
        and calls ``adapter.close()`` on exit.
        ``None`` when ``daytona_api_key`` is unset — callers treat this
        as "sandbox not available for this deployment".
    """
    if settings.daytona_api_key is None:
        return None

    # Capture the settings reference so the factory closure is self-contained.
    _settings = settings

    @asynccontextmanager
    async def _factory() -> AsyncGenerator[SandboxPort, None]:
        from openbot.infrastructure.sandboxes.daytona import DaytonaSandboxAdapter

        sem = _get_semaphore()
        _logger.debug("sandbox_factory: waiting for semaphore (concurrency cap=2)")
        async with sem:
            _logger.debug("sandbox_factory: acquired semaphore, creating sandbox")
            adapter = await DaytonaSandboxAdapter.create(settings=_settings)
            try:
                yield adapter
            finally:
                await adapter.close()

    return _factory  # type: ignore[return-value]


__all__ = ["build_sandbox_factory"]
