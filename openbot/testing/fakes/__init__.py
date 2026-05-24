"""Fake implementations of openbot.application.ports.* protocols.

Each fake mirrors its port Protocol exactly (verified by a module-level
_PROTOCOL_CHECK assignment). Observable state is exposed as immutable
tuples of frozen dataclasses; no `.calls: list[dict]` weak typing.
Failure injection is explicit (constructor kwargs), never via env vars.
"""

from __future__ import annotations

from openbot.testing.fakes.audit_log import AuditEntry, FakeAuditLog
from openbot.testing.fakes.cancellation import FakeCancellation
from openbot.testing.fakes.channel_adapter import (
    BranchRecord,
    CheckRunRecord,
    FakeChannelAdapter,
    LabelRecord,
    PROpenRecord,
    ReplyRecord,
    ReviewRecord,
)
from openbot.testing.fakes.config_loader import FakeConfigLoader
from openbot.testing.fakes.dedup import DedupRecord, FakeDedup
from openbot.testing.fakes.llm import FakeLLM, LLMCall
from openbot.testing.fakes.queue import EnqueueRecord, FakeQueue
from openbot.testing.fakes.rate_limiter import FakeRateLimiter, RateLimitRecord
from openbot.testing.fakes.resource_lock import FakeResourceLock, LockRecord
from openbot.testing.fakes.runs_repo import FakeRunsRepo, TransitionRecord
from openbot.testing.fakes.sandbox import ExecCall, FakeSandbox
from openbot.testing.fakes.sandbox_cache import CacheOp, FakeSandboxCache

__all__ = [
    "AuditEntry",
    "BranchRecord",
    "CacheOp",
    "CheckRunRecord",
    "DedupRecord",
    "EnqueueRecord",
    "ExecCall",
    "FakeAuditLog",
    "FakeCancellation",
    "FakeChannelAdapter",
    "FakeConfigLoader",
    "FakeDedup",
    "FakeLLM",
    "FakeQueue",
    "FakeRateLimiter",
    "FakeResourceLock",
    "FakeRunsRepo",
    "FakeSandbox",
    "FakeSandboxCache",
    "LLMCall",
    "LabelRecord",
    "LockRecord",
    "PROpenRecord",
    "RateLimitRecord",
    "ReplyRecord",
    "ReviewRecord",
    "TransitionRecord",
]
