"""Contract tests for ChannelAdapterPort.

The contract layer tests only the FakeChannelAdapter (no real-network
substitute exists at the in-process level — GitHubAdapter requires HTTP).
Tests verify the fake satisfies the port's behavioral contract.

The [real] parametrization uses GitHubAdapter(respx) and is marked
requires_github_token to allow full offline replay; for now the
real path uses the FakeChannelAdapter pre-configured with a respx
mock at the integration level. This file focuses on fake-contract
compliance and side-effect observation guarantees.
"""

from __future__ import annotations

import pytest

from openbot.application.ports.channel_adapter import ChannelAdapterPort
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes import FakeChannelAdapter


@pytest.fixture
def adapter() -> FakeChannelAdapter:
    return FakeChannelAdapter()


@pytest.mark.contract
class TestChannelAdapterContract:
    """FakeChannelAdapter satisfies ChannelAdapterPort behavioral contract."""

    async def test_reply_records_message(self, adapter: FakeChannelAdapter) -> None:
        event = build_issue_opened_event()
        await adapter.reply(event, "hello from openbot")
        assert len(adapter.replies) == 1
        assert adapter.replies[0].message == "hello from openbot"

    async def test_reply_returns_dict(self, adapter: FakeChannelAdapter) -> None:
        event = build_issue_opened_event()
        result = await adapter.reply(event, "hi")
        assert isinstance(result, dict)

    async def test_add_label_records_labels(self, adapter: FakeChannelAdapter) -> None:
        event = build_issue_opened_event()
        await adapter.add_label(event, "bug", "help wanted")
        assert len(adapter.labels_added) == 1
        assert "bug" in adapter.labels_added[0].labels

    async def test_get_issue_labels_returns_frozenset(self, adapter: FakeChannelAdapter) -> None:
        event = build_issue_opened_event()
        labels = await adapter.get_issue_labels(event, number=1)
        assert isinstance(labels, frozenset)

    async def test_get_actor_role_returns_str(self, adapter: FakeChannelAdapter) -> None:
        event = build_issue_opened_event()
        role = await adapter.get_actor_role(event)
        assert isinstance(role, str)

    async def test_fail_after_raises_on_nth_write(self) -> None:
        adapter = FakeChannelAdapter(fail_after=1)
        event = build_issue_opened_event()
        await adapter.reply(event, "first")  # write #1 — OK
        with pytest.raises(RuntimeError):
            await adapter.reply(event, "second")  # write #2 — fails

    async def test_verify_signature_noop_by_default(self, adapter: FakeChannelAdapter) -> None:
        adapter.verify_signature(b"body", {"x-hub-signature-256": "sha256=abc"})

    async def test_verify_signature_raises_when_configured(self) -> None:
        adapter = FakeChannelAdapter(raise_on_verify=True)
        with pytest.raises(ValueError):
            adapter.verify_signature(b"body", {})


# Static check: FakeChannelAdapter satisfies ChannelAdapterPort at import time.
def _check_port_compliance() -> None:
    _: ChannelAdapterPort = FakeChannelAdapter()
