"""Integration-layer conftest.

One fixture per application port. Each fixture yields a fresh fake
with no shared state — tests assemble their own SUTs from the fakes
they need (see docs/superpowers/specs/…tests-rebuild-design §8.3).

Forbidden: autouse IO, mega-fixtures that pre-wire multiple ports,
real network calls, subprocess spawning.
"""

from __future__ import annotations

import pytest

from openbot.testing.fakes import (
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
)


@pytest.fixture
def fake_audit_log() -> FakeAuditLog:
    """Fresh FakeAuditLog for each test."""
    return FakeAuditLog()


@pytest.fixture
def fake_cancellation() -> FakeCancellation:
    """Fresh FakeCancellation for each test."""
    return FakeCancellation()


@pytest.fixture
def fake_channel() -> FakeChannelAdapter:
    """Fresh FakeChannelAdapter for each test."""
    return FakeChannelAdapter()


@pytest.fixture
def fake_config_loader() -> FakeConfigLoader:
    """Fresh FakeConfigLoader for each test (baked-in defaults)."""
    return FakeConfigLoader()


@pytest.fixture
def fake_dedup() -> FakeDedup:
    """Fresh FakeDedup for each test."""
    return FakeDedup()


@pytest.fixture
def fake_llm() -> FakeLLM:
    """Fresh FakeLLM for each test (callers must set .responses)."""
    return FakeLLM()


@pytest.fixture
def fake_queue() -> FakeQueue:
    """Fresh FakeQueue for each test."""
    return FakeQueue()


@pytest.fixture
def fake_rate_limiter() -> FakeRateLimiter:
    """Fresh FakeRateLimiter for each test."""
    return FakeRateLimiter()


@pytest.fixture
def fake_resource_lock() -> FakeResourceLock:
    """Fresh FakeResourceLock for each test."""
    return FakeResourceLock()


@pytest.fixture
def fake_runs_repo() -> FakeRunsRepo:
    """Fresh FakeRunsRepo for each test (callers must set .responses)."""
    return FakeRunsRepo()


@pytest.fixture
def fake_sandbox() -> FakeSandbox:
    """Fresh FakeSandbox for each test."""
    return FakeSandbox()


@pytest.fixture
def fake_sandbox_cache() -> FakeSandboxCache:
    """Fresh FakeSandboxCache for each test."""
    return FakeSandboxCache()
