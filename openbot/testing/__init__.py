"""Test doubles for OpenBot.

Importable via `from openbot.testing import FakeQueue, build_issue_opened_event`.
Requires `pip install openbot[testing]`. Production install does NOT bundle
fakeredis / aiosqlite / vcrpy / respx — those are in the `testing` extra.

Layer rule: production code in openbot.{domain,application,infrastructure,
entrypoints,core,dispatcher,evaluation} MUST NOT import from this package.
This is enforced by import-linter (.importlinter contract `no-testing-in-runtime`).
The `evals/` tree IS allowed to import from here.
"""

from __future__ import annotations

from openbot.testing.fakes import (
    AuditEntry,
    BranchRecord,
    CacheOp,
    CheckRunRecord,
    DedupRecord,
    EnqueueRecord,
    ExecCall,
    FakeAuditLog,
    FakeCancellation,
    FakeChannelAdapter,
    FakeConfigLoader,
    FakeDedup,
    FakeLLM,
    FakeQueue,
    FakeRateLimiter,
    FakeResourceLock,
    FakeRunsRepo,
    FakeSandbox,
    FakeSandboxCache,
    LabelRecord,
    LLMCall,
    LockRecord,
    PROpenRecord,
    RateLimitRecord,
    ReplyRecord,
    ReviewRecord,
    TransitionRecord,
)

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
