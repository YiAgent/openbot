from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from openbot.core.settings import get_settings


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_db_init_main_exits_nonzero_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENBOT_POSTGRES_URL", raising=False)

    mod = importlib.import_module("openbot.entrypoints.cli.db_init")
    result = await mod._main([])

    assert result != 0


@pytest.mark.asyncio
async def test_db_init_main_creates_schema_for_sqlite_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "openbot.db"
    monkeypatch.setenv("OPENBOT_POSTGRES_URL", f"sqlite+aiosqlite:///{db_path}")

    mod = importlib.import_module("openbot.entrypoints.cli.db_init")
    result = await mod._main([])

    assert result == 0

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
    finally:
        await engine.dispose()

    assert "audit_log" in tables
    assert "cost_meter" in tables
    assert "task_runs" in tables
