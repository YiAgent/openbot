"""E2E-layer conftest.

Provides the ``e2e_stack`` fixture: a single-process wired stack with
a fake GitHub channel adapter and in-memory persistence.

Forbidden: real network calls, real docker, real LLM calls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.e2e._assemble import E2EStack, build_e2e_stack


@pytest.fixture
async def e2e_stack() -> AsyncIterator[E2EStack]:
    """Yield a fully wired single-process E2E stack."""
    stack = await build_e2e_stack()
    yield stack
