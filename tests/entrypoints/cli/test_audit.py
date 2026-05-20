"""`openbot audit` CLI — read-only reports over cost_meter + audit_log."""

from __future__ import annotations

import importlib
import io
import json
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from openbot.core.settings import get_settings
from openbot.domain.workflows import Feature
from openbot.infrastructure.persistence import (
    AuditLog,
    CostMeter,
    CostStatus,
    Workflow,
    WorkflowPhase,
    create_schema,
    make_engine,
    make_session_factory,
)


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create a SQLite DB seeded with a known set of cost + audit rows.

    Returns the URL string the CLI will consume via OPENBOT_POSTGRES_URL.
    """
    db_path = tmp_path / "openbot-audit.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("OPENBOT_POSTGRES_URL", url)
    engine = make_engine(url)
    try:
        await create_schema(engine)
        factory = make_session_factory(engine)
        async with factory() as session:
            # Two repos, three features, mix of cost_status:
            #   alpha/foo TRIAGE: 2 RECORDED @ $0.10 + $0.20 + 1 PRICING_FAILED
            #   alpha/foo REVIEW: 1 RECORDED @ $1.50
            #   beta/bar  CHAT:   1 RECORDED @ $0.05
            now = datetime.now(UTC)
            rows = [
                CostMeter(
                    repo="alpha/foo",
                    feature=Feature.TRIAGE,
                    task_id="t1",
                    model="anthropic/claude-haiku-4-5",
                    prompt_tokens=100,
                    completion_tokens=50,
                    cost_usd=Decimal("0.10"),
                    cost_status=CostStatus.RECORDED,
                    created_at=now - timedelta(days=1),
                ),
                CostMeter(
                    repo="alpha/foo",
                    feature=Feature.TRIAGE,
                    task_id="t2",
                    model="anthropic/claude-haiku-4-5",
                    prompt_tokens=200,
                    completion_tokens=80,
                    cost_usd=Decimal("0.20"),
                    cost_status=CostStatus.RECORDED,
                    created_at=now - timedelta(days=2),
                ),
                CostMeter(
                    repo="alpha/foo",
                    feature=Feature.TRIAGE,
                    task_id="t3",
                    model="anthropic/claude-haiku-4-5",
                    prompt_tokens=300,
                    completion_tokens=120,
                    cost_usd=Decimal("0"),  # cost unknown — must NOT add to total
                    cost_status=CostStatus.PRICING_FAILED,
                    created_at=now - timedelta(days=3),
                ),
                CostMeter(
                    repo="alpha/foo",
                    feature=Feature.REVIEW,
                    task_id="t4",
                    model="anthropic/claude-opus-4-7",
                    prompt_tokens=1000,
                    completion_tokens=400,
                    cost_usd=Decimal("1.50"),
                    cost_status=CostStatus.RECORDED,
                    created_at=now - timedelta(days=4),
                ),
                CostMeter(
                    repo="beta/bar",
                    feature=Feature.CHAT,
                    task_id="t5",
                    model="anthropic/claude-haiku-4-5",
                    prompt_tokens=20,
                    completion_tokens=10,
                    cost_usd=Decimal("0.05"),
                    cost_status=CostStatus.RECORDED,
                    created_at=now - timedelta(days=5),
                ),
                # Outside the 30-day window — must NOT show up in `cost --since 30`.
                CostMeter(
                    repo="alpha/foo",
                    feature=Feature.TRIAGE,
                    task_id="t6_old",
                    model="anthropic/claude-haiku-4-5",
                    prompt_tokens=999,
                    completion_tokens=999,
                    cost_usd=Decimal("9.99"),
                    cost_status=CostStatus.RECORDED,
                    created_at=now - timedelta(days=60),
                ),
            ]
            for r in rows:
                session.add(r)

            # Audit rows
            audits = [
                AuditLog(
                    delivery_id="d1",
                    repo="alpha/foo",
                    actor="alice",
                    workflow=Workflow.TRIAGE,
                    phase=WorkflowPhase.COMPLETED,
                    outcome="triage_done",
                    created_at=now - timedelta(hours=1),
                ),
                AuditLog(
                    delivery_id="d2",
                    repo="alpha/foo",
                    actor="bob",
                    workflow=Workflow.REVIEW,
                    phase=WorkflowPhase.FAILED,
                    outcome="llm_timeout",
                    created_at=now - timedelta(hours=2),
                ),
                AuditLog(
                    delivery_id="d3",
                    repo="beta/bar",
                    actor="carol",
                    workflow=Workflow.CHAT,
                    phase=WorkflowPhase.STARTED,
                    outcome=None,
                    created_at=now - timedelta(hours=3),
                ),
            ]
            for a in audits:
                session.add(a)
            await session.commit()
    finally:
        await engine.dispose()
    return url


@pytest.mark.asyncio
async def test_cost_subcommand_aggregates_and_filters_degraded(seeded_db: str) -> None:
    mod = importlib.import_module("openbot.entrypoints.cli.audit")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mod._run(["cost", "--since", "30", "--json"])
    assert rc == 0
    data = json.loads(buf.getvalue())

    # 3 (repo, feature) groups within 30 days. Old row at -60d is excluded.
    groups = {(r["repo"], r["feature"]): r for r in data}
    assert set(groups.keys()) == {
        ("alpha/foo", "triage"),
        ("alpha/foo", "review"),
        ("beta/bar", "chat"),
    }

    triage = groups["alpha/foo", "triage"]
    # 3 calls in window (2 recorded + 1 pricing_failed); cost only from RECORDED ($0.30).
    assert triage["calls"] == 3
    assert Decimal(triage["cost_usd"]) == Decimal("0.30")
    assert triage["degraded_calls"] == 1
    assert triage["prompt_tokens"] == 600
    assert triage["completion_tokens"] == 250

    review = groups["alpha/foo", "review"]
    assert review["calls"] == 1
    assert Decimal(review["cost_usd"]) == Decimal("1.50")
    assert review["degraded_calls"] == 0

    chat = groups["beta/bar", "chat"]
    assert chat["calls"] == 1
    assert Decimal(chat["cost_usd"]) == Decimal("0.05")


@pytest.mark.asyncio
async def test_cost_subcommand_filters_repo(seeded_db: str) -> None:
    mod = importlib.import_module("openbot.entrypoints.cli.audit")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mod._run(["cost", "--repo", "beta/bar", "--since", "30", "--json"])
    assert rc == 0
    data = json.loads(buf.getvalue())
    assert {r["repo"] for r in data} == {"beta/bar"}


@pytest.mark.asyncio
async def test_cost_markdown_has_total_line(seeded_db: str) -> None:
    mod = importlib.import_module("openbot.entrypoints.cli.audit")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mod._run(["cost", "--since", "30"])
    assert rc == 0
    out = buf.getvalue()
    assert "## Cost since" in out
    assert "| repo |" in out
    # 0.30 (triage) + 1.50 (review) + 0.05 (chat) = 1.85; pricing_failed excluded.
    assert "$1.8500" in out
    assert "RECORDED only" in out


@pytest.mark.asyncio
async def test_log_subcommand_lists_recent_newest_first(seeded_db: str) -> None:
    mod = importlib.import_module("openbot.entrypoints.cli.audit")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mod._run(["log", "--since", "7", "--json"])
    assert rc == 0
    data = json.loads(buf.getvalue())
    delivery_ids = [r["delivery_id"] for r in data]
    # Newest first: d1 (-1h), d2 (-2h), d3 (-3h).
    assert delivery_ids == ["d1", "d2", "d3"]


@pytest.mark.asyncio
async def test_log_subcommand_filters_by_phase(seeded_db: str) -> None:
    mod = importlib.import_module("openbot.entrypoints.cli.audit")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mod._run(["log", "--phase", "failed", "--since", "7", "--json"])
    assert rc == 0
    data = json.loads(buf.getvalue())
    assert {r["delivery_id"] for r in data} == {"d2"}
    assert data[0]["outcome"] == "llm_timeout"


@pytest.mark.asyncio
async def test_log_limit_caps_rows(seeded_db: str) -> None:
    mod = importlib.import_module("openbot.entrypoints.cli.audit")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mod._run(["log", "--since", "7", "--limit", "2", "--json"])
    assert rc == 0
    data = json.loads(buf.getvalue())
    assert len(data) == 2


@pytest.mark.asyncio
async def test_cli_returns_2_without_postgres_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENBOT_POSTGRES_URL", raising=False)
    mod = importlib.import_module("openbot.entrypoints.cli.audit")
    rc = await mod._run(["cost"])
    assert rc == 2


@pytest.mark.asyncio
async def test_cost_empty_db_renders_friendly_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "empty.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("OPENBOT_POSTGRES_URL", url)
    engine = make_engine(url)
    try:
        await create_schema(engine)
    finally:
        await engine.dispose()

    mod = importlib.import_module("openbot.entrypoints.cli.audit")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = await mod._run(["cost", "--since", "30"])
    assert rc == 0
    assert "No cost_meter rows" in buf.getvalue()


@pytest.mark.asyncio
async def test_audit_module_loadable() -> None:
    """Boot smoke — module loads and exposes a callable entry point."""
    mod = importlib.import_module("openbot.entrypoints.cli.audit")
    assert callable(mod.main)
