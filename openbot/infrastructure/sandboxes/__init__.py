"""Sandbox adapters — production fix loop + snapshot cache backends."""

from openbot.infrastructure.sandboxes.cache_noop import NoOpSandboxCache
from openbot.infrastructure.sandboxes.daytona import DaytonaSandboxAdapter
from openbot.infrastructure.sandboxes.fake import FakeSandboxAdapter

__all__ = ["DaytonaSandboxAdapter", "FakeSandboxAdapter", "NoOpSandboxCache"]
