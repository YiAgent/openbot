"""SQLAlchemy-backed AuditLogPort."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openbot.infrastructure.persistence.repository import AuditLogRepo

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from openbot.application.ports.audit_log import AuditLogPort


class SqlAuditLog:
    """Opens a session per write — callers don't manage session lifecycle."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

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
        async with self._session_factory() as session:
            await AuditLogRepo(session).write(
                phase=phase,
                delivery_id=delivery_id,
                repo=repo,
                actor=actor,
                workflow=workflow,
                outcome=outcome,
                details=details,
            )
            await session.commit()


if TYPE_CHECKING:
    _witness: AuditLogPort = SqlAuditLog(session_factory=lambda: None)  # type: ignore[arg-type, return-value]
