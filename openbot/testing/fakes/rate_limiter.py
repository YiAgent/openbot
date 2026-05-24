"""FakeRateLimiter — in-memory RateLimiterPort.

Default behaviour: always allows (returns ``True``). Inject specific
outcomes via ``responses=[…]`` — each call pops the front item.

Failure injection: ``fail_after=N`` raises ``fail_with(…)`` once the
call count reaches ``N`` (sticky).

All-defaults constructor: ``FakeRateLimiter()`` works with no args.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from openbot.application.ports.rate_limiter import RateLimiterPort


@dataclass(frozen=True)
class RateLimitRecord:
    """Snapshot of one check() call. All fields immutable."""

    key: str
    limit: int
    window_seconds: int
    result: bool


@dataclass
class FakeRateLimiter:
    """In-memory RateLimiterPort. Construct fresh per test."""

    responses: list[bool] = field(default_factory=list)
    fail_after: int | None = None
    fail_with: type[Exception] = RuntimeError

    _checks: list[RateLimitRecord] = field(default_factory=list, init=False)

    @property
    def checks(self) -> tuple[RateLimitRecord, ...]:
        """All check() calls in order, as an immutable tuple."""
        return tuple(self._checks)

    async def check(self, key: str, *, limit: int, window_seconds: int) -> bool:
        """See :class:`openbot.application.ports.rate_limiter.RateLimiterPort`."""
        self._maybe_fail()
        result = self.responses.pop(0) if self.responses else True
        self._checks.append(
            RateLimitRecord(key=key, limit=limit, window_seconds=window_seconds, result=result)
        )
        return result

    def _maybe_fail(self) -> None:
        if self.fail_after is not None and len(self._checks) >= self.fail_after:
            raise self.fail_with("FakeRateLimiter: simulated failure")


# Static check: import-time error if FakeRateLimiter stops satisfying RateLimiterPort.
_PROTOCOL_CHECK: Final[RateLimiterPort] = FakeRateLimiter()


__all__ = ["FakeRateLimiter", "RateLimitRecord"]
