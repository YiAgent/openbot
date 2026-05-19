"""Contract test — FakeCancellation respects CancellationPort semantics."""

from __future__ import annotations

import pytest

from tests._fakes.cancellation import FakeCancellation


@pytest.mark.asyncio
async def test_signal_then_is_cancelled() -> None:
    c = FakeCancellation()
    assert await c.is_cancelled("r1") is False
    await c.signal("r1")
    assert await c.is_cancelled("r1") is True
    assert c.signal_calls == ["r1"]
