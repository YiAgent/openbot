"""Contract test — FakeDedup conforms to DedupPort."""

from __future__ import annotations

import pytest

from openbot.application.ports.dedup import DedupPort
from openbot.infrastructure.persistence.dedup import DedupOutcome
from tests._fakes.dedup import FakeDedup


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeDedup(), DedupPort)


@pytest.mark.asyncio
async def test_fresh_then_duplicate() -> None:
    d = FakeDedup()
    assert await d.check_and_mark("github", "delivery-1") == DedupOutcome.FRESH
    assert await d.check_and_mark("github", "delivery-1") == DedupOutcome.DUPLICATE
