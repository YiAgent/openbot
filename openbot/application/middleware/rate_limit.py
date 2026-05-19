"""Chat-feature rate limiting — PRD §4.6 / harness spec §3 M6.

Two Redis counters per chat request:

  - ``rl:user:{actor_login}:{YYYY-MM-DD}``    INCR + EXPIRE 86400
  - ``rl:repo:{repo_full}:{YYYY-MM-DD-HH}``   INCR + EXPIRE 3600

If `event.actor` resolves to an exempt role (default: owner /
collaborator), both counters are skipped entirely — exempt users
should not even consume the quota by accident.

The `cost_cap_per_task` field on RateLimitConfig is enforced inside
the agent loop, not here; the task hasn't started yet.

Safety:

  - **Redis key injection**: GitHub login is validated against
    ``^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`` (the documented GitHub
    username grammar) before composing the key. A bad login skips
    the user counter rather than building an arbitrary Redis key.

  - **Actor role lookup**: cached on ``ctx.cache`` so this middleware
    and the slice-C ActorRoleMiddleware share the single API call.

  - **Fall-open on Redis errors**: same reasoning as
    `WebhookDedup` — blocking every chat when Redis flaps is worse
    than maybe-overspending the daily quota by a few requests.

Comments are emitted at most once per window via `announce_once`
(§9.2 locked): one per user per day, one per repo per hour.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Final

from openbot.application.middleware.preflight import (
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
)
from openbot.domain.workflows import Feature, WorkflowPhase

_logger = logging.getLogger(__name__)

# GitHub username grammar (per their docs): 1-39 chars, alnum or hyphens,
# can't start with hyphen. Reject anything outside this before composing
# a Redis key — defense against `actor = "u:..."` slipping a colon into
# the namespace and stomping unrelated keys.
_GITHUB_LOGIN_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")

# Repo full_name: `{owner}/{name}` — owner follows login grammar; name
# allows alnum + hyphens + dots + underscores. Slash separator is
# expected and validated by the regex.
_GITHUB_REPO_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")

_ACTOR_ROLE_CACHE_KEY: Final = "actor_role.value"


async def _resolve_actor_role(ctx: PreflightContext) -> str:
    """Adapter call, cached on ctx so slice-C reuses it.

    Returns one of admin / maintain / write / triage / read / none.
    Errors → "none" + WARNING (treats unknown actors as non-exempt).
    """
    cached = ctx.cache.get(_ACTOR_ROLE_CACHE_KEY)
    if cached is not None:
        return cached
    try:
        role = await ctx.adapter.get_actor_role(ctx.event)
    except Exception:
        _logger.exception(
            "actor_role_lookup_failed",
            extra={"actor": ctx.event.actor, "repo": ctx.event.repo},
        )
        role = "none"
    ctx.cache[_ACTOR_ROLE_CACHE_KEY] = role
    return role


class RateLimitMiddleware:
    """Per-user-per-day + per-repo-per-hour limits for the chat feature."""

    name = "rate_limit"

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        # Chat-only — other features have their own budget paths (PRD
        # §4.5) and don't accumulate per-user quotas.
        if ctx.dispatch.feature is not Feature.CHAT:
            return MiddlewareDecision.proceed()

        if ctx.rate_limiter is None:
            # No rate limiter = no rate-limit (slice A's dedup falls open the
            # same way). Audit at INFO; production sets OPENBOT_REDIS_URL.
            _logger.info(
                "rate_limit_skipped_no_rate_limiter",
                extra={"delivery_id": ctx.event.delivery_id, "repo": ctx.event.repo},
            )
            return MiddlewareDecision.proceed()

        # Exempt roles bypass both counters. Per PRD §4.6 default is
        # `[owner, collaborator]`; the user can shrink or expand via
        # config.chat.rate_limit.exempt_roles.
        cfg = ctx.config.rate_limit
        role = await _resolve_actor_role(ctx)
        if role in cfg.exempt_roles:
            return MiddlewareDecision.proceed()

        now = datetime.now(UTC)

        # ── per_user_per_day ─────────────────────────────────────────
        actor = ctx.event.actor or ""
        if _GITHUB_LOGIN_RE.fullmatch(actor):
            day = now.strftime("%Y-%m-%d")
            user_key = f"rl:user:{actor}:{day}"
            try:
                count_allowed = await ctx.rate_limiter.check(
                    user_key, limit=cfg.per_user_per_day, window_seconds=86400
                )
            except Exception:
                _logger.exception(
                    "rate_limit_user_check_failed_fail_open",
                    extra={"actor": actor, "repo": ctx.event.repo},
                )
                count_allowed = True  # fall-open
            if not count_allowed:
                return MiddlewareDecision(
                    result=MiddlewareResult.BLOCKED,
                    reason="rate_limit_per_user_per_day",
                    audit_phase=WorkflowPhase.SKIPPED,
                    comment=(
                        f"⏳ Rate limited: {cfg.per_user_per_day}/day reached. Resets at 00:00 UTC."
                    ),
                    announce_key=f"rate:user:{actor}:{day}",
                    announce_ttl=86400,
                )
        else:
            # Malformed actor → skip user counter, still check repo.
            _logger.warning(
                "rate_limit_actor_login_invalid_skipping_user_counter",
                extra={"actor_raw": actor[:64], "repo": ctx.event.repo},
            )

        # ── per_repo_per_hour ────────────────────────────────────────
        repo = ctx.event.repo or ""
        if _GITHUB_REPO_RE.fullmatch(repo):
            hour = now.strftime("%Y-%m-%d-%H")
            repo_key = f"rl:repo:{repo}:{hour}"
            try:
                repo_allowed = await ctx.rate_limiter.check(
                    repo_key, limit=cfg.per_repo_per_hour, window_seconds=3600
                )
            except Exception:
                _logger.exception(
                    "rate_limit_repo_check_failed_fail_open",
                    extra={"repo": repo},
                )
                repo_allowed = True  # fall-open
            if not repo_allowed:
                return MiddlewareDecision(
                    result=MiddlewareResult.BLOCKED,
                    reason="rate_limit_per_repo_per_hour",
                    audit_phase=WorkflowPhase.SKIPPED,
                    comment=(
                        f"⏳ Rate limited: {cfg.per_repo_per_hour}/hour reached "
                        f"on this repo. Try again next hour."
                    ),
                    announce_key=f"rate:repo:{repo}:{hour}",
                    announce_ttl=3600,
                )
        else:
            _logger.warning(
                "rate_limit_repo_invalid_skipping_repo_counter",
                extra={"repo_raw": repo[:128]},
            )

        return MiddlewareDecision.proceed()


__all__ = ["RateLimitMiddleware"]
