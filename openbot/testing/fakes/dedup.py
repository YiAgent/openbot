"""FakeDedup — in-memory DedupPort.

Default behaviour mirrors the real adapter: first call per
``(channel, delivery_id)`` returns ``FRESH``; every subsequent call
for the same pair returns ``DUPLICATE``. Override with
``responses=[…]`` to inject specific ``DedupOutcome`` values — each
call pops the front item (the automatic seen-set is bypassed when the
list is non-empty).

Failure injection: ``fail_after=N`` raises ``fail_with(…)`` once the
call count reaches ``N`` (sticky).

All-defaults constructor: ``FakeDedup()`` works with no args.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from openbot.application.ports.dedup import DedupPort
from openbot.domain.dedup import DedupOutcome


@dataclass(frozen=True)
class DedupRecord:
    """Snapshot of one check_and_mark() call. All fields immutable."""

    channel: str
    delivery_id: str
    outcome: DedupOutcome


@dataclass
class FakeDedup:
    """In-memory DedupPort. Construct fresh per test."""

    responses: list[DedupOutcome] = field(default_factory=list)
    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _checks: list[DedupRecord] = field(default_factory=list, init=False)
    _seen: set[tuple[str, str]] = field(default_factory=set, init=False)

    @property
    def checks(self) -> tuple[DedupRecord, ...]:
        """All check_and_mark() calls in order, as an immutable tuple."""
        return tuple(self._checks)

    async def check_and_mark(self, channel: str, delivery_id: str) -> DedupOutcome:
        """See :class:`openbot.application.ports.dedup.DedupPort`."""
        self._maybe_fail()
        if self.responses:
            outcome = self.responses.pop(0)
        else:
            key = (channel, delivery_id)
            if key in self._seen:
                outcome = DedupOutcome.DUPLICATE
            else:
                self._seen.add(key)
                outcome = DedupOutcome.FRESH
        self._checks.append(DedupRecord(channel=channel, delivery_id=delivery_id, outcome=outcome))
        return outcome

    def _maybe_fail(self) -> None:
        if self.fail_after is not None and len(self._checks) >= self.fail_after:
            raise self.fail_with("FakeDedup: simulated failure")


# Static check: import-time error if FakeDedup stops satisfying DedupPort.
_PROTOCOL_CHECK: Final[DedupPort] = FakeDedup()


__all__ = ["DedupRecord", "FakeDedup"]
