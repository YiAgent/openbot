"""Contract test — FakeAuditLog records every field."""

from __future__ import annotations

import pytest

from tests._fakes.audit_log import FakeAuditLog


@pytest.mark.asyncio
async def test_write_persists_kwargs() -> None:
    a = FakeAuditLog()
    await a.write(
        phase="started",
        delivery_id="d1",
        repo="owner/repo",
        actor="user1",
        workflow="triage",
        outcome=None,
        details={"foo": "bar"},
    )
    assert a.rows == [
        {
            "phase": "started",
            "delivery_id": "d1",
            "repo": "owner/repo",
            "actor": "user1",
            "workflow": "triage",
            "outcome": None,
            "details": {"foo": "bar"},
        }
    ]
