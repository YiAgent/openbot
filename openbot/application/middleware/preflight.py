"""Pre-flight middleware framework — harness spec §3 M3 + §9.2.

Slice A scope: types + runner + `announce_once` helper. No real gates
yet. Gates land in slice B (cancel, budget, rate-limit) and slice C
(fork-PR, actor-role, prompt-injection wrapping).

Design notes:

  - `PreflightContext` is **frozen**: middlewares cannot mutate it. If
    a middleware needs to share state with later middlewares (e.g.
    cached actor role), put it on the redis client or a future
    `MutableContext` companion — keep the immutable view immutable.

  - `MiddlewareDecision` carries `announce_key` + `announce_ttl` rather
    than letting middlewares fire-and-forget GitHub API calls. This
    keeps the runner the single place that does I/O for BLOCKED paths,
    so audit ordering is deterministic (audit row first, then optional
    comment) and tests can swap one helper.

  - `run_preflight` short-circuits on the first BLOCKED. PROCEED keeps
    walking. This is the only execution order in v0.1; if we ever want
    "warn but continue" we add a third `MiddlewareResult` value rather
    than overloading PROCEED.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from openbot.domain.events import UnifiedEvent
from openbot.infrastructure.persistence.models import WorkflowPhase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import redis.asyncio as redis_async
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from openbot.application.router import Dispatch
    from openbot.domain.config_schema import EffectiveConfig
    from openbot.infrastructure.adapters.github import GitHubAdapter

_logger = logging.getLogger(__name__)


class MiddlewareResult(StrEnum):
    """Two-state outcome from a single middleware.

    PROCEED  → keep walking the chain.
    BLOCKED  → stop; the runner audit-logs and (optionally) replies.
    """

    PROCEED = "proceed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class MiddlewareDecision:
    """One middleware's verdict.

    Fields:
      result:        PROCEED | BLOCKED.
      reason:        Short, log-stable identifier (e.g. ``"per_user_per_day"``).
                     Goes into `audit_log.outcome`; do not include user content.
      comment:       Optional message to reply on the issue/PR. Sent only on
                     BLOCKED. Pass `None` to suppress the reply entirely.
      announce_key:  When set, the runner posts `comment` only once per key —
                     `SET NX EX <ttl>` on `openbot:announced:{key}`. Spec §9.2.
                     `None` means "always reply" (used by §9.4 cancel-comment).
      announce_ttl:  Seconds the announce-key persists. Ignored if
                     `announce_key` is None.
      audit_phase:   Which `WorkflowPhase` the BLOCKED row records. Defaults
                     to SKIPPED (a soft gate); set REJECTED for hard gates
                     like signature mismatch or kill-switch.
    """

    result: MiddlewareResult
    reason: str | None = None
    comment: str | None = None
    announce_key: str | None = None
    announce_ttl: int = 0
    audit_phase: WorkflowPhase = WorkflowPhase.SKIPPED

    @classmethod
    def proceed(cls) -> MiddlewareDecision:
        """Canonical PROCEED — no audit row, no comment."""
        return cls(result=MiddlewareResult.PROCEED)


@dataclass(frozen=True, slots=True)
class PreflightContext:
    """Immutable snapshot of one webhook handling, plus shared handles.

    `redis` and `session_factory` may be None when the corresponding
    backend is not configured (dev mode without docker-compose). Each
    middleware is responsible for gracefully falling open in that case.
    """

    event: UnifiedEvent
    dispatch: Dispatch
    config: EffectiveConfig
    adapter: GitHubAdapter
    session_factory: async_sessionmaker[AsyncSession] | None
    redis: redis_async.Redis | None
    # GitHub check_run_id — when present, the workflow handler may update
    # it with detailed logs or status changes.
    check_run_id: int | None = None
    # Reserved for slice B+: middlewares may stash cached lookups
    # (actor role, cancel set membership) keyed by their middleware name.
    # Frozen at construction; slice A leaves it empty.
    cache: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@runtime_checkable
class Middleware(Protocol):
    """Single async callable + a name for audit/logs.

    Implementations should never raise — convert internal errors into
    a PROCEED with a WARNING log, because the rest of the pipeline
    treats raised exceptions as 5xx (which makes GitHub retry the
    webhook and is rarely what a gate-failure ought to do).
    """

    name: str

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision: ...


async def run_preflight(
    ctx: PreflightContext,
    middlewares: Iterable[Middleware],
) -> MiddlewareDecision:
    """Walk `middlewares` in order; return first BLOCKED or a fresh PROCEED.

    On BLOCKED:
      1. Write an `audit_log` row with `phase=<decision.audit_phase>` and
         `outcome=<decision.reason>` so ops can see WHICH middleware blocked.
      2. If `comment` is set, post it via `announce_once` when `announce_key`
         is also set; otherwise post unconditionally (cancel-comment path).
      3. Return the decision unchanged.
    """
    blocking_name: str | None = None
    decision: MiddlewareDecision = MiddlewareDecision.proceed()
    for mw in middlewares:
        try:
            decision = await mw(ctx)
        except Exception:
            _logger.exception(
                "preflight_middleware_raised",
                extra={
                    "middleware": mw.name,
                    "delivery_id": ctx.event.delivery_id,
                    "repo": ctx.event.repo,
                },
            )
            # A buggy middleware must not turn into a 5xx that GitHub
            # retries forever. Treat it as PROCEED + audit, slice B/C
            # gates each protect themselves so this safety net is rarely
            # touched in practice.
            continue
        if decision.result is MiddlewareResult.BLOCKED:
            blocking_name = mw.name
            break

    if decision.result is MiddlewareResult.BLOCKED:
        await _write_block_audit(ctx, decision, middleware_name=blocking_name)
        if decision.comment:
            await _post_block_comment(ctx, decision)

    return decision


async def announce_once(
    redis: redis_async.Redis | None,
    *,
    key: str,
    ttl_seconds: int,
    reply: Callable[[], Awaitable[Any]],
) -> bool:
    """Post `reply()` only the first time `key` is seen within `ttl_seconds`.

    Returns True iff the reply was sent. Fall-closed behavior: when Redis
    is unavailable we return False and do NOT post — better to skip a
    notification than to spam.
    """
    if redis is None:
        return False
    full_key = f"openbot:announced:{key}"
    try:
        was_set = await redis.set(full_key, "1", nx=True, ex=ttl_seconds)
    except Exception:
        _logger.exception("announce_once_redis_error", extra={"key": key})
        return False
    if not was_set:
        return False
    try:
        await reply()
        return True
    except Exception:
        _logger.exception("announce_once_reply_failed", extra={"key": key})
        return False


async def _write_block_audit(
    ctx: PreflightContext,
    decision: MiddlewareDecision,
    *,
    middleware_name: str | None,
) -> None:
    if ctx.session_factory is None:
        # No Postgres configured — drop, the dev environment already logs.
        return
    # Local import: keeps the module importable in unit tests that
    # don't pull in SQLAlchemy.
    from openbot.infrastructure.persistence.repository import AuditLogRepo

    workflow_value = ctx.dispatch.feature.value
    details = {
        "middleware": middleware_name,
        "kind": ctx.event.kind.value,
    }
    try:
        async with ctx.session_factory() as session:
            await AuditLogRepo(session).write(
                phase=decision.audit_phase.value,
                delivery_id=ctx.event.delivery_id or None,
                repo=ctx.event.repo or None,
                actor=ctx.event.actor or None,
                workflow=workflow_value,
                outcome=decision.reason,
                details=details,
            )
            await session.commit()
    except Exception:
        _logger.exception(
            "preflight_audit_write_failed",
            extra={
                "delivery_id": ctx.event.delivery_id,
                "repo": ctx.event.repo,
                "middleware": middleware_name,
            },
        )


async def _post_block_comment(ctx: PreflightContext, decision: MiddlewareDecision) -> None:
    comment = decision.comment
    if not comment:
        return

    async def reply() -> Any:
        return await ctx.adapter.reply(ctx.event, comment)

    if decision.announce_key is None:
        # §9.4: cancel-comment path always replies — wrap the call so a
        # GitHub API hiccup never propagates as a 5xx out of pre-flight.
        try:
            await reply()
        except Exception:
            _logger.exception(
                "preflight_comment_failed",
                extra={"delivery_id": ctx.event.delivery_id, "repo": ctx.event.repo},
            )
        return

    await announce_once(
        ctx.redis,
        key=decision.announce_key,
        ttl_seconds=decision.announce_ttl,
        reply=reply,
    )
