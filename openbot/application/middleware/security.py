"""Security gates — PRD §4.8 + §4.3 / harness spec §3 M7.

Two middlewares:

  - ``ForkPRGateMiddleware``
        For PR_OPENED / PR_SYNCHRONIZED events whose head repo is NOT
        the base repo (i.e. a fork), drop the workflow unless a
        maintainer (admin / maintain / write role) has commented the
        ``ok_to_test_phrase`` (default ``/ok-to-test``) on the PR.

        Why: a fork PR can run untrusted code in CI / sandbox under
        our installation token. PRD §4.8 locks this default to "off"
        and requires an opt-in by someone who already has write rights
        on the repo.

  - ``ActorRoleMiddleware``
        For FIX feature: enforce ``config.fix.allowed_actors`` (default
        ``[collaborator, owner]``) — random outside contributors can't
        assign the bot to issues and have a $3 task run.

        For CHAT feature: if ``config.chat.allow_anyone`` is False,
        require the same actor-role gate. Default (True) is open.

        Other features (TRIAGE / REVIEW) pass through — they're
        triggered by impersonal events (issue.opened / pr.opened) where
        the actor's role doesn't affect what the workflow does.

Both rely on ``ctx.adapter.get_actor_role(event)`` which slice B's
RateLimitMiddleware already cached on ``ctx.cache``; this middleware
re-uses the same key so the API call happens once per webhook.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from openbot.application.middleware.preflight import (
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
)
from openbot.domain.events import EventKind
from openbot.domain.workflows import Feature, WorkflowPhase

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)


# Reuse the cache key slice B's rate_limit.py set so we don't refetch
# the role for the same webhook. Inlining the literal avoids a circular
# import on slice B's private constant.
_ACTOR_ROLE_CACHE_KEY: Final = "actor_role.value"

# Roles considered "maintainer-equivalent" for fork-PR ok-to-test
# authorization (PRD §4.8). `triage` and `read` are NOT in this set
# — neither lets a user run untrusted code today, so allowing them
# to greenlight a fork would be a privilege escalation.
_MAINTAINER_ROLES: Final = frozenset({"admin", "maintain", "write"})

# Default roles allowed to trigger FIX. Overridable via
# config.fix.allowed_actors (not yet modeled on EffectiveConfig as
# of slice C; defaulting here mirrors PRD §4.3 lock).
_FIX_ALLOWED_ROLES: Final = frozenset({"collaborator", "owner", "admin", "maintain", "write"})


async def _resolve_actor_role(ctx: PreflightContext) -> str:
    """Same shape as slice B's helper. Cached on ctx.cache so both
    rate-limit and security gates share the single API call.
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


# ───────────────────────── Fork PR gate ─────────────────────────


class ForkPRGateMiddleware:
    """Default-deny fork PRs unless a maintainer wrote ``/ok-to-test``.

    The "is this a fork" check inspects the webhook payload's
    ``pull_request.head.repo.full_name`` vs ``base.repo.full_name``.
    Same repo → not a fork (early PROCEED). Different repo → fork →
    check for the opt-in.

    Opt-in is granted via a PR-level *issue comment* containing the
    phrase as its first non-whitespace content, posted by an account
    with admin / maintain / write role on the base repo. Each comment
    that grants access is logged for audit.

    Caches:
      - the "is fork" result (set on every call)
      - the list of issue comments (if we had to fetch it)
    Both keys live on ctx.cache and are scoped to this webhook only.
    """

    name = "fork_pr_gate"

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        # Only PR events have a head/base repo distinction. Slice A
        # only dispatches REVIEW for PR_OPENED / PR_SYNCHRONIZED, but
        # belt-and-suspenders the check anyway.
        if ctx.event.kind not in (EventKind.PR_OPENED, EventKind.PR_SYNCHRONIZED):
            return MiddlewareDecision.proceed()
        if ctx.dispatch.feature is not Feature.REVIEW:
            return MiddlewareDecision.proceed()

        is_fork = self._is_fork_pr(ctx)
        if not is_fork:
            return MiddlewareDecision.proceed()

        if not ctx.config.fork_pr.run:
            # Default v0.1 stance: fork_pr.run is False. We DO check
            # for /ok-to-test even when the global toggle is off,
            # because that toggle is "auto-run fork PRs"; the comment
            # is the documented manual override per PRD §4.8.
            phrase = ctx.config.fork_pr.ok_to_test_phrase
            if await self._has_maintainer_ok_to_test(ctx, phrase=phrase):
                return MiddlewareDecision.proceed()

            _logger.info(
                "fork_pr_blocked_no_ok_to_test",
                extra={
                    "delivery_id": ctx.event.delivery_id,
                    "repo": ctx.event.repo,
                    "pr_number": ctx.event.pr_number,
                    "actor": ctx.event.actor,
                },
            )
            return MiddlewareDecision(
                result=MiddlewareResult.BLOCKED,
                reason="fork_pr_no_ok_to_test",
                audit_phase=WorkflowPhase.SKIPPED,
                comment=(
                    "🔒 OpenBot does not run on fork PRs by default. "
                    "A maintainer (admin / write / maintain role) can "
                    f"enable this run by commenting `{ctx.config.fork_pr.ok_to_test_phrase}` "
                    "on this PR."
                ),
                # 1 reply per PR — once a fork PR has been told it's
                # blocked, syncing new commits to it shouldn't spam
                # the same notice on every push.
                announce_key=f"forkpr:{ctx.event.repo}:{ctx.event.pr_number}",
                announce_ttl=30 * 86400,
            )

        # Auto-run forks (fork_pr.run == True) — admin override; PROCEED.
        return MiddlewareDecision.proceed()

    def _is_fork_pr(self, ctx: PreflightContext) -> bool:
        """Inspect head vs base repo on the PR payload.

        Two equivalent signals on the GitHub payload — we accept either
        being true because some replay scenarios mutate `fork: bool`
        but not `full_name`, and vice-versa:

          1. ``head.repo.fork == True``                     (explicit fork flag)
          2. ``head.repo.full_name != base.repo.full_name`` (different repos)
        """
        pr = ctx.event.raw.get("pull_request") or {}
        head = (pr.get("head") or {}).get("repo") or {}
        base = (pr.get("base") or {}).get("repo") or {}
        head_full = head.get("full_name")
        base_full = base.get("full_name")
        if head.get("fork") is True:
            return True
        return bool(head_full and base_full and head_full != base_full)

    async def _has_maintainer_ok_to_test(self, ctx: PreflightContext, *, phrase: str) -> bool:
        """List PR comments + accept any whose body starts with `phrase`
        and whose author has admin/maintain/write on the base repo.

        Comment search is bounded to the most recent 100 comments (one
        API page) — a maintainer who needs to greenlight a fork PR
        typically does it close to the trigger event; a long-tail comment
        stays unsupported until they re-comment.
        """
        comments = await self._fetch_pr_comments(ctx)
        if not comments:
            return False

        phrase_norm = phrase.strip().lower()
        for comment in comments:
            body = (comment.get("body") or "").strip().lower()
            if not body.startswith(phrase_norm):
                continue
            author = (comment.get("user") or {}).get("login")
            if not author:
                continue
            role = await self._role_for(ctx, login=author)
            if role in _MAINTAINER_ROLES:
                _logger.info(
                    "fork_pr_ok_to_test_granted",
                    extra={
                        "delivery_id": ctx.event.delivery_id,
                        "repo": ctx.event.repo,
                        "pr_number": ctx.event.pr_number,
                        "by": author,
                        "role": role,
                    },
                )
                return True
        return False

    async def _fetch_pr_comments(self, ctx: PreflightContext) -> list[dict[str, Any]]:
        """Most recent 100 PR comments. Cached on ctx.cache for this webhook."""
        cache_key = "fork_pr.comments"
        cached = ctx.cache.get(cache_key)
        if cached is not None:
            return cached
        if ctx.event.pr_number is None:
            return []
        url = (
            f"{ctx.adapter._api_base}/repos/{ctx.event.repo}/issues/"
            f"{ctx.event.pr_number}/comments?per_page=100"
        )
        try:
            data = await ctx.adapter._authed_json("GET", url, ctx.event)
        except Exception:
            _logger.exception(
                "fork_pr_comments_fetch_failed",
                extra={"delivery_id": ctx.event.delivery_id, "repo": ctx.event.repo},
            )
            return []
        result = list(data) if isinstance(data, list) else []
        ctx.cache[cache_key] = result
        return result

    async def _role_for(self, ctx: PreflightContext, *, login: str) -> str:
        """Look up *another* actor's role on the repo (not the event actor)."""
        url = f"{ctx.adapter._api_base}/repos/{ctx.event.repo}/collaborators/{login}/permission"
        try:
            data = await ctx.adapter._authed_json("GET", url, ctx.event)
        except Exception:
            return "none"
        return str(data.get("permission") or "none") if isinstance(data, dict) else "none"


# ───────────────────────── Actor role gate ─────────────────────────


class ActorRoleMiddleware:
    """Enforce per-feature actor allow-lists.

    FIX  → only collaborator/owner/admin/maintain/write can assign the
           bot to an issue and have a real $3 task run.

    CHAT → if ``config.chat.allow_anyone`` is False (rare; default True),
           apply the same allow-list.

    TRIAGE / REVIEW → impersonal triggers; PROCEED.
    """

    name = "actor_role"

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        feature = ctx.dispatch.feature
        if feature is Feature.FIX:
            return await self._check_role(ctx, allowed=_FIX_ALLOWED_ROLES, label="fix")

        if feature is Feature.CHAT:
            # `allow_anyone` lives on raw config for now; default open.
            chat_section = ctx.config.raw.get("chat") if ctx.config.raw else None
            allow_anyone = True
            if isinstance(chat_section, dict):
                allow_anyone = bool(chat_section.get("allow_anyone", True))
            if allow_anyone:
                return MiddlewareDecision.proceed()
            return await self._check_role(ctx, allowed=_FIX_ALLOWED_ROLES, label="chat")

        return MiddlewareDecision.proceed()

    async def _check_role(
        self, ctx: PreflightContext, *, allowed: frozenset[str], label: str
    ) -> MiddlewareDecision:
        role = await _resolve_actor_role(ctx)
        if role in allowed:
            return MiddlewareDecision.proceed()
        _logger.info(
            "actor_role_blocked",
            extra={
                "delivery_id": ctx.event.delivery_id,
                "feature": label,
                "actor": ctx.event.actor,
                "role": role,
            },
        )
        # No reply: silently dropping is the spec for unauthorized
        # actors — replying would teach an attacker which actors are
        # almost-but-not-quite authorized (probing surface).
        return MiddlewareDecision(
            result=MiddlewareResult.BLOCKED,
            reason=f"actor_role_not_allowed_{label}",
            audit_phase=WorkflowPhase.REJECTED,
        )


__all__ = ["ActorRoleMiddleware", "ForkPRGateMiddleware"]
