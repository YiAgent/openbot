"""Contract test — FakeChannelAdapter conforms to ChannelAdapterPort."""

from __future__ import annotations

import pytest

from openbot.application.ports.channel_adapter import ChannelAdapterPort
from openbot.domain.events import EventKind
from tests._fakes.channel_adapter import FakeChannelAdapter


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeChannelAdapter(), ChannelAdapterPort)


@pytest.mark.asyncio
async def test_reply_records_message() -> None:
    fa = FakeChannelAdapter()
    ev = fa.parse_event(b"", {})
    assert ev.kind == EventKind.UNKNOWN
    out = await fa.reply(ev, "hello")
    assert out["ok"] is True
    assert fa.replies == [(ev.resource_key, "hello")]
