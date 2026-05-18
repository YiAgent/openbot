"""Budget enforcement middleware — PRD §4.5 / harness spec §3 M5.

Input-side check: does the workflow have permission to **start**?

  - ``global_hard_kill``    Cross-repo monthly ceiling. Env
                            ``OPENBOT_GLOBAL_HARD_KILL_USD`` overrides the
                            config value so an admin can drop the cap
                            without a redeploy. BLOCKED with no reply —
                            the whole instance is paused; admin job.

  - ``monthly_soft_cap``    Per-repo monthly ceiling. BLOCKED + 1-per-month
                            comment via ``announce_once`` so the
                            maintainer learns once, not on every webhook
                            for the rest of the month.

The per-task cap can't be enforced here — the task hasn't started, so
the running total is always 0. That check moves into the agent loop
once it ships.

Spend math uses ``sum_recorded_*`` (RECORDED rows only). Degraded rows
(``pricing_failed``, ``usage_missing``, ``priced_zero``) contribute 0 to
their own `cost_usd` and would silently leak headroom if we counted
them. Spec §3 M5 locks this.

Falls open when the DB session factory is unavailable (no Postgres
configured): logs WARNING + audits + proceeds. The alternative —
hard-failing every workflow when Postgres is down — is worse than
maybe-overspending for the duration of one outage.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from openbot.application.middleware.preflight import (
    MiddlewareDecision,
    MiddlewareResult,
    PreflightContext,
)
from openbot.infrastructure.persistence.models import WorkflowPhase
from openbot.infrastructure.persistence.repository import CostMeterRepo, rolling_month_window

_logger = logging.getLogger(__name__)

_GLOBAL_HARD_KILL_ENV: Final = "OPENBOT_GLOBAL_HARD_KILL_USD"


def _effective_global_cap(default: Decimal) -> Decimal:
    """Env override wins; bad values fall back to config default + WARNING."""
    raw = os.environ.get(_GLOBAL_HARD_KILL_ENV)
    if raw is None or raw == "":
        return default
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        _logger.warning(
            "global_hard_kill_env_bad_value",
            extra={"raw": raw[:64], "fell_back_to_config": str(default)},
        )
        return default


class BudgetMiddleware:
    """Checks `global_hard_kill_usd` then `monthly_soft_cap_usd`.

    Order matters: global first so admins can stop the world without
    waiting for every repo's soft cap to also trip. The decisions are
    independent — we don't combine messages, each gate audits its own
    reason.
    """

    name = "budget"

    async def __call__(self, ctx: PreflightContext) -> MiddlewareDecision:
        if ctx.session_factory is None:
            # No Postgres configured. Slice A's webapp wires this when
            # `OPENBOT_POSTGRES_URL` is set; without it, budget math
            # is meaningless. Fall open + audit at INFO.
            _logger.info(
                "budget_skipped_no_postgres",
                extra={"delivery_id": ctx.event.delivery_id, "repo": ctx.event.repo},
            )
            return MiddlewareDecision.proceed()

        now = datetime.now(UTC)
        window_start = rolling_month_window(now)

        try:
            async with ctx.session_factory() as session:
                repo_obj = CostMeterRepo(session)
                global_spend = await repo_obj.sum_recorded_global_since(window_start)
                repo_spend = await repo_obj.sum_recorded_for_repo_since(
                    ctx.event.repo, window_start
                )
        except Exception:
            # DB transient errors → fall-open (same reasoning as the
            # dedup module's fall-open: blocking every webhook on a
            # blip is worse than maybe-overspending for one event).
            _logger.exception(
                "budget_db_query_failed_fail_open",
                extra={"delivery_id": ctx.event.delivery_id, "repo": ctx.event.repo},
            )
            return MiddlewareDecision.proceed()

        global_cap = _effective_global_cap(ctx.config.budget.global_hard_kill_usd)
        if global_spend >= global_cap:
            _logger.warning(
                "budget_global_hard_kill",
                extra={
                    "delivery_id": ctx.event.delivery_id,
                    "global_spend_usd": str(global_spend),
                    "cap_usd": str(global_cap),
                },
            )
            return MiddlewareDecision(
                result=MiddlewareResult.BLOCKED,
                reason="global_hard_kill",
                audit_phase=WorkflowPhase.REJECTED,
                # No comment: admin job. Replying via GitHub while we're
                # globally killed risks compounding outages — the API
                # might be the very thing the kill switch is in place
                # to avoid hammering.
            )

        soft_cap = ctx.config.budget.monthly_soft_cap_usd
        if repo_spend >= soft_cap:
            month_label = now.strftime("%Y-%m")
            _logger.warning(
                "budget_monthly_soft_cap",
                extra={
                    "delivery_id": ctx.event.delivery_id,
                    "repo": ctx.event.repo,
                    "repo_spend_usd": str(repo_spend),
                    "cap_usd": str(soft_cap),
                },
            )
            return MiddlewareDecision(
                result=MiddlewareResult.BLOCKED,
                reason="monthly_soft_cap",
                audit_phase=WorkflowPhase.SKIPPED,
                comment=(
                    f"⚠️ OpenBot 已用完该仓库本月预算 (${soft_cap}). "
                    f"已用 ${repo_spend}, 下月初恢复.\n\n"
                    f"⚠️ Monthly budget exhausted for this repo (cap: "
                    f"${soft_cap}, spent: ${repo_spend}). Workflows pause "
                    f"until next month."
                ),
                # Per §9.2: spec-locked 1-per-month dedup, ~31d TTL.
                announce_key=f"budget:{ctx.event.repo}:{month_label}",
                announce_ttl=31 * 86400,
            )

        return MiddlewareDecision.proceed()


__all__ = ["BudgetMiddleware"]
