"""SQLAlchemy-backed RunsRepoPort implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openbot.application.state.runs_repo import TransitionResult, transition

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from openbot.application.ports.runs_repo import RunsRepoPort
    from openbot.domain.events import UnifiedEvent


class SqlRunsRepo:
    """Wraps the free transition() with a session-per-call pattern."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def transition(self, *, event: UnifiedEvent, new_run_id: str) -> TransitionResult:
        async with self._session_factory() as session:
            result = await transition(session, event=event, new_run_id=new_run_id)
            await session.commit()
            return result


if TYPE_CHECKING:
    _witness: RunsRepoPort = SqlRunsRepo(session_factory=lambda: None)  # type: ignore[arg-type, return-value]
