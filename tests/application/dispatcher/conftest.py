"""Dispatcher test fixtures.

Auto-mock litellm.acompletion so tests that exercise decide_and_enqueue
do not make real LLM calls. Tests that explicitly want to control the
classifier output should patch litellm.acompletion themselves (which
overrides this autouse fixture for that test).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_litellm_acompletion():
    """Prevent real LiteLLM calls in dispatcher tests.

    The classifier is fail-open: any exception returns None and the
    dispatcher continues on the classifier_skipped path. Returning an
    empty-JSON response mimics the "classifier ran, returned nothing
    useful" path without hitting any real endpoint.
    """
    message = MagicMock()
    message.content = json.dumps({"priority": "p2", "labels": [], "stages": []})
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        yield
