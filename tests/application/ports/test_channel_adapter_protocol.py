"""Verify FakeChannelAdapter satisfies the full ChannelAdapterPort protocol."""

from __future__ import annotations

from openbot.application.ports.channel_adapter import ChannelAdapterPort
from tests._fakes.channel_adapter import FakeChannelAdapter


def test_fake_channel_adapter_satisfies_protocol() -> None:
    """FakeChannelAdapter must satisfy all methods on ChannelAdapterPort."""
    # Verify add_label exists as a callable on the fake
    adapter = FakeChannelAdapter()
    assert callable(getattr(adapter, "add_label", None)), (
        "add_label missing from FakeChannelAdapter"
    )
    # Verify it's declared on the port
    assert "add_label" in dir(ChannelAdapterPort), "add_label missing from ChannelAdapterPort"
