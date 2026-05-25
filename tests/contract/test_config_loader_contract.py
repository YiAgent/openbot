"""Contract tests for ConfigLoaderPort.

[fake] uses FakeConfigLoader with baked-in defaults, [real] uses
YamlConfigLoader with a FakeChannelAdapter that returns no .openbot.yml
(simulating a repo without config — should produce baked-in defaults).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openbot.application.ports.config_loader import ConfigLoaderPort
from openbot.domain.config_schema import EffectiveConfig
from openbot.infrastructure.config_loader import YamlConfigLoader
from openbot.testing.builders.events import build_issue_opened_event
from openbot.testing.fakes import FakeChannelAdapter, FakeConfigLoader


@pytest.fixture(params=["fake", "real"])
async def config_loader(request: pytest.FixtureRequest) -> AsyncIterator[ConfigLoaderPort]:
    if request.param == "fake":
        yield FakeConfigLoader()
    else:
        # YamlConfigLoader with a fake channel that has no .openbot.yml
        yield YamlConfigLoader()


@pytest.mark.contract
class TestConfigLoaderContract:
    async def test_returns_effective_config(self, config_loader: ConfigLoaderPort) -> None:
        adapter = FakeChannelAdapter(fake_file=None)  # no .openbot.yml
        event = build_issue_opened_event()
        config = await config_loader.load_for_repo(adapter, event)
        assert isinstance(config, EffectiveConfig)

    async def test_config_has_features_attr(self, config_loader: ConfigLoaderPort) -> None:
        adapter = FakeChannelAdapter(fake_file=None)
        event = build_issue_opened_event()
        config = await config_loader.load_for_repo(adapter, event)
        assert hasattr(config, "features")

    async def test_config_has_budget_attr(self, config_loader: ConfigLoaderPort) -> None:
        adapter = FakeChannelAdapter(fake_file=None)
        event = build_issue_opened_event()
        config = await config_loader.load_for_repo(adapter, event)
        assert hasattr(config, "budget")
