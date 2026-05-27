"""Evaluation integration test fixtures.

The evaluation runner tests mock the agent responders, but LangChain's
Anthropic integration validates credentials at client-construction time even
when the model is never called. Provide a dummy key so the SDK doesn't raise
AuthenticationError before the responder mock fires.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fake_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a syntactically valid placeholder API key for the test process."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-placeholder-00000000")
