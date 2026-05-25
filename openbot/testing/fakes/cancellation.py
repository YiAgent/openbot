"""FakeCancellation — in-memory CancellationPort.

``cancelled`` is the injectable set of pre-cancelled run IDs AND
accumulates every ``signal()`` call. ``signalled`` is the observation
tuple of only the IDs explicitly passed to ``signal()`` — this lets
tests distinguish pre-injected cancellations from in-flight ones.

Failure injection: ``fail_after=N`` raises ``fail_with(…)`` once the
signal() call count reaches ``N`` (sticky).

All-defaults constructor: ``FakeCancellation()`` works with no args.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from openbot.application.ports.cancellation import CancellationPort


@dataclass
class FakeCancellation:
    """In-memory CancellationPort. Construct fresh per test."""

    cancelled: set[str] = field(default_factory=set)
    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _signals: list[str] = field(default_factory=list, init=False)

    @property
    def signalled(self) -> tuple[str, ...]:
        """All run_ids passed to signal() in order, as an immutable tuple."""
        return tuple(self._signals)

    async def signal(self, run_id: str) -> None:
        """See :class:`openbot.application.ports.cancellation.CancellationPort`."""
        self._maybe_fail()
        self._signals.append(run_id)
        self.cancelled.add(run_id)

    async def is_cancelled(self, run_id: str) -> bool:
        """See :class:`openbot.application.ports.cancellation.CancellationPort`."""
        return run_id in self.cancelled

    def _maybe_fail(self) -> None:
        if self.fail_after is not None and len(self._signals) >= self.fail_after:
            raise self.fail_with("FakeCancellation: simulated failure")


# Static check: import-time error if FakeCancellation stops satisfying CancellationPort.
_PROTOCOL_CHECK: Final[CancellationPort] = FakeCancellation()


__all__ = ["FakeCancellation"]
