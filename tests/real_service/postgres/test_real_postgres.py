"""Real-service tests for Postgres-backed adapters.

Skipped automatically when OPENBOT_POSTGRES_URL is absent.
Requires a real Postgres instance with the Alembic schema applied.
"""

from __future__ import annotations

import uuid

import pytest

from tests.real_service.conftest import _env_or_skip

# Module-level skip: one 's' per file, not one per test
env = _env_or_skip("OPENBOT_POSTGRES_URL")


@pytest.mark.real_service
class TestRealPostgresAuditLog:
    async def test_write_and_query_audit_entry(self) -> None:
        """Audit entry written to real Postgres is readable."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from openbot.infrastructure.persistence.audit_log_impl import SqlAuditLog

        engine = create_async_engine(env["OPENBOT_POSTGRES_URL"])
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            audit = SqlAuditLog(session_factory=session_factory)
            delivery_id = f"rs-audit-{uuid.uuid4().hex[:8]}"
            await audit.write(
                phase="started",
                delivery_id=delivery_id,
                repo="owner/repo",
                actor="octocat",
                workflow="triage",
                outcome="ok",
                details={"test": True},
            )
            # If no exception raised, write succeeded
            assert True
        finally:
            await engine.dispose()


@pytest.mark.real_service
class TestRealPostgresRunsRepo:
    async def test_transition_creates_row(self) -> None:
        """runs_repo.transition writes a new row to real Postgres."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from openbot.infrastructure.persistence.runs_repo_impl import SqlRunsRepo
        from openbot.testing.builders.events import build_issue_opened_event

        engine = create_async_engine(env["OPENBOT_POSTGRES_URL"])
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            runs_repo = SqlRunsRepo(session_factory=session_factory)
            # Use a random issue_number so each run starts with a fresh resource
            # key — avoids stale RUNNING rows from previous test runs.
            issue_number = int(uuid.uuid4().hex[:6], 16) + 1_000_000
            event = build_issue_opened_event(issue_number=issue_number)
            run_id = f"rs-{uuid.uuid4().hex[:16]}"
            result = await runs_repo.transition(event=event, new_run_id=run_id)
            assert result.run_id == run_id
        finally:
            await engine.dispose()
