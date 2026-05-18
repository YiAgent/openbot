"""FakeAuditLog — in-memory AuditLogPort."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeAuditLog:
    """Records every write() call in `rows`."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    async def write(
        self,
        *,
        phase: str,
        delivery_id: str | None = None,
        repo: str | None = None,
        actor: str | None = None,
        workflow: str | None = None,
        outcome: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.rows.append(
            {
                "phase": phase,
                "delivery_id": delivery_id,
                "repo": repo,
                "actor": actor,
                "workflow": workflow,
                "outcome": outcome,
                "details": details or {},
            }
        )
