"""AuditLogPort — append-only writer; observable order."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openbot.application.ports.audit_log import AuditLogPort
from openbot.infrastructure.persistence.audit_log_impl import SqlAuditLog
from openbot.testing.fakes.audit_log import FakeAuditLog
from openbot.testing.inmemory.postgres import build_inmemory_db


@pytest.fixture(params=["fake", "real"])
async def audit(request: pytest.FixtureRequest) -> AsyncIterator[AuditLogPort]:
    if request.param == "fake":
        yield FakeAuditLog()
    else:
        async with build_inmemory_db() as sf:
            yield SqlAuditLog(session_factory=sf)


@pytest.mark.contract
class TestAuditLogContract:
    async def test_write_does_not_raise(self, audit: AuditLogPort) -> None:
        await audit.write(phase="started", outcome="ok")

    async def test_optional_fields_are_accepted(self, audit: AuditLogPort) -> None:
        await audit.write(
            phase="completed",
            delivery_id="d1",
            repo="owner/r",
            actor="octocat",
            workflow="review",
            outcome="ok",
            details={"k": "v"},
        )
