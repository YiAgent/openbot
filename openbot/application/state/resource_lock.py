"""Per-resource Redis lock — receive-side mutual exclusion.

The webhook receiver acquires this lock around the
``classify → CAS-write TaskRun → enqueue`` sequence so two concurrent
deliveries for the same issue/PR can't race past each other into a
double-START. The CAS check on ``TaskRun.row_version`` (see
``state.runs_repo``) is the belt; this lock is the suspenders — it keeps
the contention window essentially zero so the CAS retry is just
defensive scaffolding for TTL expiry edge cases.

Implementation:

  - ``SET key token NX EX <ttl>`` — atomic acquire. ``token`` is a random
    8-byte hex value owned by the caller; stored as the lock's value so
    operators can grep the holder when debugging.
  - 3-attempt back-off (100 ms between attempts) before giving up.
    Real contention is rare; this absorbs the brief overlap when GitHub
    fires two webhooks ~10 ms apart for a duplicate retry.
  - Fall-open when ``redis is None`` (unit tests, ``make dev`` without
    docker-compose). The receive side still functions, just without the
    cross-process guard — the CAS column is the safety net.

Key shape: ``openbot:resource_lock:{resource_key}``. Stable + greppable
so an operator can ``redis-cli KEYS "openbot:resource_lock:*"`` to see
which resources are mid-classification.

Release path uses a plain ``DEL`` rather than a compare-and-delete: the
TTL is short (10 s) and our protected section is <50 ms, so the
"TTL expired → other holder → we DEL theirs" race window is vanishingly
small in practice. The ``TaskRun.row_version`` CAS catches the case if
it ever fires.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import redis.asyncio as redis_async

_logger = logging.getLogger(__name__)

_KEY_PREFIX: Final = "openbot:resource_lock"

# 10s TTL — far longer than a realistic classify+enqueue (<50 ms) but
# short enough that a crashed receiver doesn't pin the resource until
# Redis is restarted. The CAS retry on ``runs_repo.transition`` handles
# the rare case where the TTL expires before we release.
_DEFAULT_TTL_SECONDS: Final = 10

# Up to 3 attempts (initial + 2 retries) with a 100ms back-off. Bursts of
# duplicate retries from GitHub are the main contention source, and they
# usually clear inside a few hundred ms. Keeping the cap tight means the
# webhook still responds well inside GitHub's 10s timeout even when the
# lock is held.
_MAX_ATTEMPTS: Final = 3
_BACKOFF_SECONDS: Final = 0.1


def _key(resource_key: str) -> str:
    return f"{_KEY_PREFIX}:{resource_key}"


@asynccontextmanager
async def resource_lock(
    redis: redis_async.Redis | None,
    resource_key: str,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> AsyncGenerator[bool, None]:
    """Async context manager that holds the per-resource lock.

    Yields ``True`` when the lock is held (acquired or fallback-open),
    ``False`` when we couldn't acquire after ``_MAX_ATTEMPTS``. Callers
    that REQUIRE mutual exclusion should check the yielded value and
    bail; for our receive-side flow we still proceed on ``False``
    because the CAS column rejects double-writes anyway — the lock is
    just to keep retries cheap.

    Usage::

        async with resource_lock(redis, "github:owner/repo:pr:42") as acquired:
            if not acquired:
                _logger.warning("lock_contention", ...)
            # classify + CAS write under the lock either way
    """
    if redis is None:
        # No Redis configured — degrade open. The CAS column on
        # ``task_runs`` will catch the rare double-write.
        yield True
        return

    key = _key(resource_key)
    token = secrets.token_hex(8)
    acquired = False

    for attempt in range(_MAX_ATTEMPTS):
        try:
            was_set = await redis.set(key, token, nx=True, ex=ttl_seconds)
        except Exception:
            # Redis flap — fall open, same rationale as WebhookDedup.
            _logger.exception(
                "resource_lock_redis_error_fail_open",
                extra={"resource_key": resource_key, "attempt": attempt + 1},
            )
            yield True
            return

        if was_set:
            acquired = True
            break
        if attempt + 1 < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_SECONDS)

    if not acquired:
        _logger.warning(
            "resource_lock_contention",
            extra={"resource_key": resource_key, "attempts": _MAX_ATTEMPTS},
        )
        # Yield False so the caller can audit-log the miss, but DO NOT
        # release on exit (we never held it).
        yield False
        return

    try:
        yield True
    finally:
        try:
            await redis.delete(key)
        except Exception:
            # Release failures are non-fatal — TTL guarantees the lock
            # eventually clears even if our DEL never lands.
            _logger.exception(
                "resource_lock_release_failed",
                extra={"resource_key": resource_key},
            )


__all__ = ["resource_lock"]
