"""Queue test fixtures.

Auto-mock litellm.acompletion so worker tests that call consume_loop don't
make real LLM calls via classify_for_dispatch. The classify step is
fail-open (returns None on any exception), but a real network call leaves
behind an unawaited LiteLLM async-logging coroutine that pytest surfaces as
PytestUnraisableExceptionWarning on the *next* test in the session.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_litellm_acompletion():
    """Return a benign JSON classifier response; skip real LLM network calls."""
    message = MagicMock()
    message.content = json.dumps({"priority": "p2", "labels": [], "stages": []})
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        yield
