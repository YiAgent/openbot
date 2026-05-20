"""`openbot audit` CLI — report cost + audit_log from Postgres.

Read-only operator tool. Closes the v0.2-deferred item from the archived
harness spec §1.2 / §8.

Two subcommands:

    python -m openbot.entrypoints.cli.audit cost  [--repo R] [--since 30] [--json]
    python -m openbot.entrypoints.cli.audit log   [--repo R] [--since 7]  [--limit 50] [--json]
                                                  [--phase started|completed|failed|...]

Defaults aim at "show me what just happened" — `cost` summarizes the last
30 days, `log` lists the most recent 50 entries from the last 7 days.

Output is markdown (paste-into-issue friendly) unless `--json`.

Connects via `OPENBOT_POSTGRES_URL` (same setting webapp / worker use).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from openbot.core.settings import get_settings
from openbot.infrastructure.persistence import (
    AuditLog,
    CostMeter,
    CostStatus,
    make_engine,
    make_session_factory,
    session_scope,
)

_logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Data shapes (immutable views; CLI never mutates DB)
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CostRow:
    """One (repo, feature) aggregate row in the cost report."""

    repo: str
    feature: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal  # only RECORDED rows contribute
    degraded_calls: int  # rows whose cost_status != RECORDED

    def to_json(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "feature": self.feature,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": str(self.cost_usd),
            "degraded_calls": self.degraded_calls,
        }


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One audit_log row for the `log` subcommand."""

    created_at: datetime
    delivery_id: str | None
    repo: str | None
    actor: str | None
    workflow: str | None
    phase: str
    outcome: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at.isoformat(),
            "delivery_id": self.delivery_id,
            "repo": self.repo,
            "actor": self.actor,
            "workflow": self.workflow,
            "phase": self.phase,
            "outcome": self.outcome,
        }


# ──────────────────────────────────────────────────────────────────────────
# Queries
# ──────────────────────────────────────────────────────────────────────────


async def fetch_cost_rows(
    engine: AsyncEngine,
    *,
    repo: str | None,
    since: datetime,
) -> list[CostRow]:
    """Aggregate cost_meter rows by (repo, feature) since `since`.

    Cost sum filters to RECORDED rows only (matches BudgetEnforcement
    semantics — degraded rows have unknown cost). `degraded_calls` is
    surfaced as a separate column so operators can see when pricing is
    drifting without confusing it with $0 calls.
    """
    # SUM(CASE WHEN ...) — portable across postgres + sqlite (FILTER (WHERE)
    # is PG-only). Filtering cost to RECORDED rows matches BudgetEnforcement
    # semantics from the harness spec §4.5.
    recorded_cost = func.coalesce(
        func.sum(
            case(
                (CostMeter.cost_status == CostStatus.RECORDED, CostMeter.cost_usd),
                else_=0,
            )
        ),
        0,
    ).label("cost_recorded")
    degraded_calls = func.coalesce(
        func.sum(case((CostMeter.cost_status != CostStatus.RECORDED, 1), else_=0)),
        0,
    ).label("degraded")

    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        stmt = (
            select(
                CostMeter.repo,
                CostMeter.feature,
                func.count().label("calls"),
                func.coalesce(func.sum(CostMeter.prompt_tokens), 0).label("prompt"),
                func.coalesce(func.sum(CostMeter.completion_tokens), 0).label("completion"),
                recorded_cost,
                degraded_calls,
            )
            .where(CostMeter.created_at >= since)
            .group_by(CostMeter.repo, CostMeter.feature)
            .order_by(CostMeter.repo, CostMeter.feature)
        )
        if repo:
            stmt = stmt.where(CostMeter.repo == repo)
        result = await session.execute(stmt)
        rows: list[CostRow] = []
        for r_repo, r_feature, calls, prompt, completion, cost, degraded in result.all():
            rows.append(
                CostRow(
                    repo=str(r_repo),
                    feature=str(r_feature),
                    calls=int(calls),
                    prompt_tokens=int(prompt),
                    completion_tokens=int(completion),
                    cost_usd=Decimal(str(cost)),
                    degraded_calls=int(degraded),
                )
            )
        return rows


async def fetch_audit_rows(
    engine: AsyncEngine,
    *,
    repo: str | None,
    phase: str | None,
    since: datetime,
    limit: int,
) -> list[AuditRow]:
    """Most recent audit_log entries within `since`, newest first."""
    factory = make_session_factory(engine)
    async with session_scope(factory) as session:
        stmt = (
            select(AuditLog)
            .where(AuditLog.created_at >= since)
            .order_by(desc(AuditLog.created_at))
            .limit(limit)
        )
        if repo:
            stmt = stmt.where(AuditLog.repo == repo)
        if phase:
            stmt = stmt.where(AuditLog.phase == phase)
        result = await session.execute(stmt)
        return [
            AuditRow(
                created_at=r.created_at,
                delivery_id=r.delivery_id,
                repo=r.repo,
                actor=r.actor,
                workflow=str(r.workflow) if r.workflow else None,
                phase=str(r.phase),
                outcome=r.outcome,
            )
            for r in result.scalars().all()
        ]


# ──────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────


def render_cost_markdown(rows: Sequence[CostRow], *, since: datetime) -> str:
    """Markdown table grouped by repo, with grand total."""
    if not rows:
        return f"_No cost_meter rows since {since.isoformat()}._"

    # Totals across all rows (RECORDED-only cost; full call/token counts).
    totals = {
        "calls": sum(r.calls for r in rows),
        "prompt": sum(r.prompt_tokens for r in rows),
        "completion": sum(r.completion_tokens for r in rows),
        "cost": sum((r.cost_usd for r in rows), Decimal("0")),
        "degraded": sum(r.degraded_calls for r in rows),
    }

    # Per-repo subtotals for compact display.
    by_repo: dict[str, list[CostRow]] = defaultdict(list)
    for r in rows:
        by_repo[r.repo].append(r)

    out: list[str] = []
    out.append(f"## Cost since {since.isoformat()}")
    out.append("")
    out.append("| repo | feature | calls | prompt tok | completion tok | cost USD | degraded |")
    out.append("|------|---------|------:|-----------:|---------------:|---------:|---------:|")
    for repo, repo_rows in by_repo.items():
        for r in repo_rows:
            out.append(
                f"| {repo} | {r.feature} | {r.calls} | {r.prompt_tokens} | "
                f"{r.completion_tokens} | {r.cost_usd:.4f} | {r.degraded_calls} |"
            )
    out.append("")
    out.append(
        f"**Total**: {totals['calls']} calls, {totals['prompt']} prompt tok, "
        f"{totals['completion']} completion tok, "
        f"${totals['cost']:.4f} (RECORDED only), {totals['degraded']} degraded rows."
    )
    return "\n".join(out)


def render_audit_markdown(rows: Sequence[AuditRow], *, since: datetime) -> str:
    if not rows:
        return f"_No audit_log rows since {since.isoformat()}._"
    out: list[str] = []
    out.append(f"## Audit log since {since.isoformat()} (most recent first)")
    out.append("")
    out.append("| time (UTC) | phase | workflow | repo | actor | outcome |")
    out.append("|------------|-------|----------|------|-------|---------|")
    for r in rows:
        # Truncate outcome so the line wraps cleanly in a GitHub comment.
        outcome = (r.outcome or "")[:80]
        out.append(
            f"| {r.created_at.isoformat(timespec='seconds')} | {r.phase} | "
            f"{r.workflow or '—'} | {r.repo or '—'} | {r.actor or '—'} | {outcome} |"
        )
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────────
# CLI plumbing
# ──────────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openbot-audit",
        description="Read-only reports over cost_meter + audit_log.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    cost = sub.add_parser("cost", help="Cost summary grouped by (repo, feature).")
    cost.add_argument("--repo", help="Filter to a single repo (owner/name).")
    cost.add_argument(
        "--since",
        type=int,
        default=30,
        help="How many days back (default 30).",
    )
    cost.add_argument("--json", action="store_true", help="Emit JSON instead of markdown.")

    log = sub.add_parser("log", help="Recent audit_log entries.")
    log.add_argument("--repo", help="Filter to a single repo.")
    log.add_argument(
        "--phase",
        help="Filter to a single phase (e.g. started / completed / failed / rejected).",
    )
    log.add_argument(
        "--since",
        type=int,
        default=7,
        help="How many days back (default 7).",
    )
    log.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max rows to show (default 50).",
    )
    log.add_argument("--json", action="store_true", help="Emit JSON instead of markdown.")

    return p


async def _run(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    if not settings.postgres_url:
        _logger.error("audit_missing_postgres_url")
        print("OPENBOT_POSTGRES_URL is not set.", file=sys.stderr)
        return 2

    since = datetime.now(UTC) - timedelta(days=args.since)
    engine = make_engine(settings.postgres_url, echo=settings.debug)
    try:
        if args.cmd == "cost":
            rows = await fetch_cost_rows(engine, repo=args.repo, since=since)
            if args.json:
                print(json.dumps([r.to_json() for r in rows], ensure_ascii=False, indent=2))
            else:
                print(render_cost_markdown(rows, since=since))
            return 0
        if args.cmd == "log":
            rows = await fetch_audit_rows(
                engine,
                repo=args.repo,
                phase=args.phase,
                since=since,
                limit=args.limit,
            )
            if args.json:
                print(json.dumps([r.to_json() for r in rows], ensure_ascii=False, indent=2))
            else:
                print(render_audit_markdown(rows, since=since))
            return 0
    finally:
        await engine.dispose()
    return 1  # unreachable; argparse already enforced `required=True`


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(_run(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
