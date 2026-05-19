"""AuditLogPort — durable audit trail writer."""

from __future__ import annotations

from typing import Any, Protocol


class AuditLogPort(Protocol):
    """Append-only audit row writer."""

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
    ) -> None: ...
