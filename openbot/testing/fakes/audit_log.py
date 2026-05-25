"""FakeAuditLog — in-memory AuditLogPort.

Records every write() call as an immutable AuditEntry frozen dataclass.
The ``entries`` property returns all calls in order as an immutable tuple.

Failure injection: ``fail_after=N`` raises ``fail_with(…)`` once the
write() call count reaches ``N`` (sticky).

All-defaults constructor: ``FakeAuditLog()`` works with no args.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from openbot.application.ports.audit_log import AuditLogPort


@dataclass(frozen=True)
class AuditEntry:
    """Snapshot of one write() call. All fields immutable."""

    phase: str
    delivery_id: str | None = None
    repo: str | None = None
    actor: str | None = None
    workflow: str | None = None
    outcome: str | None = None
    details: dict[str, Any] | None = None


@dataclass
class FakeAuditLog:
    """In-memory AuditLogPort. Construct fresh per test."""

    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _entries: list[AuditEntry] = field(default_factory=list, init=False)

    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        """All write() calls in order, as an immutable tuple."""
        return tuple(self._entries)

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
        """See :class:`openbot.application.ports.audit_log.AuditLogPort`."""
        self._maybe_fail()
        self._entries.append(
            AuditEntry(
                phase=phase,
                delivery_id=delivery_id,
                repo=repo,
                actor=actor,
                workflow=workflow,
                outcome=outcome,
                details=details,
            )
        )

    def _maybe_fail(self) -> None:
        if self.fail_after is not None and len(self._entries) >= self.fail_after:
            raise self.fail_with("FakeAuditLog: simulated failure")


# Static check: import-time error if FakeAuditLog stops satisfying AuditLogPort.
_PROTOCOL_CHECK: Final[AuditLogPort] = FakeAuditLog()


__all__ = ["AuditEntry", "FakeAuditLog"]
