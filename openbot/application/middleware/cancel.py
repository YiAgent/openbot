"""Cancel middlewares — PRD §4.7 / harness spec §3 M4 + §9.4.

Three independent gates, each shipped as its own ``Middleware`` so that
audit rows clearly point at the responsible mechanism. Slice B covers
the **input-side** checks only; the agent-loop step-counter cancel
hook (5-step poll inside the LangGraph middleware stack) lands with
the agent slice.

  - ``kill_switch``      Env var ``OPENBOT_KILL_SWITCH=true`` halts every
                         workflow immediately. Read at check time (NOT
                         from cached Settings) so the admin can flip it
                         without restarting the process. REJECTED phase
                         + no comment (the whole instance is intentionally
                         down).

  - ``cancel_label``     If the issue/PR currently being processed has
                         the cancel label (default ``cancel-openbot``),
                         drop. Single GitHub API call per webhook; result
                         is stashed on ``ctx.cache`` so future middlewares
                         in the same request don't refetch. REJECTED, no
                         comment (the user already signaled intent by
                         applying the label).

  - ``cancel_comment``   If a chat-feature comment matches
                         ``@openbot (stop|cancel|停|取消)``, record the
                         intent in Redis (``cancel:thread:{task_id}``)
                         and BLOCKED + **always** reply (§9.4 — every
                         user-initiated cancel deserves immediate visible
                         feedback). The Redis set is what the future
                         agent-loop step-counter consumes; for now it's
                         just an audit signal.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import TYPE_CHECKING, Final

from openbot.application.middleware.preflight import (
    Middleware,
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
)
from openbot.domain.events import EventKind
from openbot.infrastructure.llm.model_router import Feature
from openbot.infrastructure.persistence.models import WorkflowPhase

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)

_KILL_SWITCH_ENV: Final = "OPENBOT_KILL_SWITCH"

# Cap comment-body parsing so an attacker pasting a 50-MB payload can't
# stall the regex engine. 4 KB covers any realistic chat command and is
# well below GitHub's own ~65 KB comment ceiling.
_COMMENT_MAX_CHARS: Final = 4096

# Anchored + length-bounded + character-class compile = ReDoS-safe.
# `re.IGNORECASE` matches STOP / Stop / stop equally; `re.UNICODE`
# (the default in Python 3) is needed so the CJK keywords match too.
# We compile once with `<keywords>` filled at first use to honor any
# user override from config.cancel.comment_keywords.
_MENTION_RE: Final = re.compile(r"^@openbot\s+(.{1,256})\s*$", re.IGNORECASE | re.DOTALL)


# ───────────────────────── kill switch ─────────────────────────


class KillSwitchMiddleware:
    """Env-var emergency stop. Goes first in the chain.

    PRD §4.7 lists this as the third cancel mechanism. We put it first
    in pre-flight because (a) it costs nothing to check, (b) when the
    admin flips it they want every webhook silenced, including those
    that would otherwise short-circuit on cancel-label or rate-limit.
    """

    name = "kill_switch"

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        if os.environ.get(_KILL_SWITCH_ENV, "").strip().lower() == "true":
            _logger.warning(
                "kill_switch_engaged",
                extra={"delivery_id": ctx.event.delivery_id, "repo": ctx.event.repo},
            )
            return MiddlewareDecision(
                result=MiddlewareResult.BLOCKED,
                reason="kill_switch",
                audit_phase=WorkflowPhase.REJECTED,
                # No comment: when the whole instance is intentionally
                # off, the GitHub API may also be unreachable and a
                # 5xx from .reply() would just add noise to the audit.
            )
        return MiddlewareDecision.proceed()


# ───────────────────────── cancel label ─────────────────────────


_LABELS_CACHE_KEY: Final = "cancel_label.labels"


class CancelLabelMiddleware:
    """Skip the workflow if the cancel label is on the issue/PR.

    Issues `GET /repos/{repo}/issues/{n}/labels` once per webhook; the
    result is cached on ``ctx.cache`` so the future ForkPRGate (slice C)
    can reuse it without a second API call.

    Falls open on GitHub API failure — better to maybe-run a workflow
    once than to block all webhooks when GitHub is flaky.
    """

    name = "cancel_label"

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        number = ctx.event.issue_number or ctx.event.pr_number
        if number is None:
            # Events with no issue/PR surface (push, star) can't carry
            # the label — slice A's Router already blocks these from
            # reaching pre-flight, but defense-in-depth is cheap.
            return MiddlewareDecision.proceed()

        cancel_label = ctx.config.cancel.label
        try:
            labels = await self._fetch_labels(ctx, number=number)
        except Exception:
            _logger.exception(
                "cancel_label_fetch_failed",
                extra={
                    "delivery_id": ctx.event.delivery_id,
                    "repo": ctx.event.repo,
                    "number": number,
                },
            )
            return MiddlewareDecision.proceed()  # fall-open

        if cancel_label not in labels:
            return MiddlewareDecision.proceed()

        _logger.info(
            "cancel_label_blocked",
            extra={
                "delivery_id": ctx.event.delivery_id,
                "repo": ctx.event.repo,
                "number": number,
                "label": cancel_label,
            },
        )
        return MiddlewareDecision(
            result=MiddlewareResult.BLOCKED,
            reason="cancel_label",
            audit_phase=WorkflowPhase.REJECTED,
        )

    async def _fetch_labels(self, ctx: PreflightContext, *, number: int) -> frozenset[str]:
        """Return the set of label names; cached on ctx.cache for the request."""
        cached = ctx.cache.get(_LABELS_CACHE_KEY)
        if cached is not None:
            return cached
        # `_authed_json` (private on GitHubAdapter) is the shortest path
        # to "GET this URL with a fresh installation token". Re-using
        # the adapter's existing machinery keeps the http/token plumbing
        # in one place; if/when ForkPRGate also needs labels (slice C),
        # it reads the same cache key.
        url = f"{ctx.adapter._api_base}/repos/{ctx.event.repo}/issues/{number}/labels"
        data = await ctx.adapter._authed_json("GET", url, ctx.event)
        names = frozenset(
            str(item["name"])
            for item in (data or [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        ctx.cache[_LABELS_CACHE_KEY] = names
        return names


# ───────────────────────── cancel comment ─────────────────────────


class CancelCommentMiddleware:
    """`@openbot stop|cancel|停|取消` on a comment → BLOCKED + audit + reply.

    Only relevant on chat-dispatched events (issue / PR review comments).
    Other features have no comment surface to drop a cancel on.

    Per spec §9.4 we ALWAYS reply (no announce_once dedup). The user
    just took an action — silence would be confusing ("did it register?").
    Two cancels in a row → two confirmation replies; cheap and clear.
    """

    name = "cancel_comment"

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        # Only chat events carry cancel-comment intent. The router only
        # dispatches CHAT for `@openbot ...` comments, so we trust the
        # dispatch — this gate has nothing to do on other features.
        if ctx.dispatch.feature is not Feature.CHAT:
            return MiddlewareDecision.proceed()
        if ctx.event.kind not in (
            EventKind.ISSUE_COMMENT_CREATED,
            EventKind.PR_REVIEW_COMMENT_CREATED,
        ):
            return MiddlewareDecision.proceed()

        body = ctx.event.comment_body or ""
        # Strip Unicode format controls (zero-width, RTL override) so
        # `@openbot​stop` doesn't bypass detection. `Cf` is the
        # general category Unicode reserves for these.
        body = "".join(ch for ch in body if unicodedata.category(ch) != "Cf")
        if len(body) > _COMMENT_MAX_CHARS:
            body = body[:_COMMENT_MAX_CHARS]

        match = _MENTION_RE.match(body)
        if match is None:
            return MiddlewareDecision.proceed()

        # Lowercase the captured token for keyword comparison. The
        # config-supplied keywords default to ("stop", "cancel", "停",
        # "取消") and are user-overridable.
        first_word = match.group(1).split(None, 1)[0].lower()
        keywords = {k.lower() for k in ctx.config.cancel.comment_keywords}
        if first_word not in keywords:
            return MiddlewareDecision.proceed()

        # Record the cancellation intent for the future agent-loop
        # step-counter. SADD is idempotent — a repeat doesn't grow the
        # set or fire a notification we'd have to suppress.
        await self._mark_thread_cancelled(ctx)

        _logger.info(
            "cancel_comment_blocked",
            extra={
                "delivery_id": ctx.event.delivery_id,
                "repo": ctx.event.repo,
                "actor": ctx.event.actor,
                "keyword": first_word,
            },
        )
        return MiddlewareDecision(
            result=MiddlewareResult.BLOCKED,
            reason="cancel_comment",
            audit_phase=WorkflowPhase.REJECTED,
            # §9.4: every user-initiated cancel deserves a confirmation
            # reply; do NOT pass announce_key so the runner replies
            # unconditionally each time.
            comment=(
                "🛑 已记录取消请求 - 当前任务 (若有正在运行的) 会在下一个"
                "检查点停止.\n"
                "🛑 Cancellation recorded - any running task will stop at "
                "its next checkpoint."
            ),
        )

    async def _mark_thread_cancelled(self, ctx: PreflightContext) -> None:
        """Add the task_id to Redis cancel set. Best-effort; no raise."""
        if ctx.redis is None:
            return
        key = f"cancel:thread:{ctx.dispatch.task_id}"
        try:
            await ctx.redis.sadd(key, "1")
            # 7d TTL — agent loops are bounded at 45 min (PRD §4.3),
            # so this is generous. Cleans up so cancelled task_ids
            # don't accumulate forever.
            await ctx.redis.expire(key, 7 * 86400)
        except Exception:
            _logger.exception(
                "cancel_thread_sadd_failed",
                extra={"task_id": ctx.dispatch.task_id, "repo": ctx.event.repo},
            )


__all__ = [
    "CancelCommentMiddleware",
    "CancelLabelMiddleware",
    "KillSwitchMiddleware",
    "Middleware",
]
