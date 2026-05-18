"""Frozen bundle of Ports the dispatcher chain may need.

Built once per process at the composition root (api `deps.py` or worker
`__main__.py`). Middleware constructors accept a `DispatcherDeps` and read
only the Ports they need.

Fields are typed as Port Protocols, never as concrete infra types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbot.application.ports.audit_log import AuditLogPort
    from openbot.application.ports.cancellation import CancellationPort
    from openbot.application.ports.channel_adapter import ChannelAdapterPort
    from openbot.application.ports.config_loader import ConfigLoaderPort
    from openbot.application.ports.dedup import DedupPort
    from openbot.application.ports.llm import LLMPort
    from openbot.application.ports.queue import QueuePort
    from openbot.application.ports.rate_limiter import RateLimiterPort
    from openbot.application.ports.resource_lock import ResourceLockPort
    from openbot.application.ports.runs_repo import RunsRepoPort
    from openbot.application.ports.sandbox import SandboxPort


@dataclass(frozen=True, slots=True)
class DispatcherDeps:
    """Every Port the chain may need. Composition root builds this once."""

    dedup: "DedupPort"
    queue: "QueuePort"
    channel: "ChannelAdapterPort"
    runs_repo: "RunsRepoPort"
    resource_lock: "ResourceLockPort"
    cancellation: "CancellationPort"
    audit: "AuditLogPort"
    rate_limiter: "RateLimiterPort"
    config_loader: "ConfigLoaderPort"
    llm: "LLMPort"
    sandbox: "SandboxPort"
