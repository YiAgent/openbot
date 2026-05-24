"""Unit tests for openbot.evaluation.runner.

Tests use monkeypatching to avoid real LLM calls. The runner's job
is to wire the correct event + adapter, not to test the responders.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from openbot.domain.fix import FixAttempt, FixOutcome
from openbot.domain.review import ReviewFindings
from openbot.evaluation.runner import (
    run_chat_sample,
    run_fix_sample,
    run_review_sample,
    run_test_generation_sample,
)

# ── run_review_sample ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_review_sample_calls_responder(monkeypatch) -> None:
    """run_review_sample builds an eval event and calls the review responder."""
    expected = ReviewFindings(summary="looks good", findings=())
    mock_responder = MagicMock()
    mock_responder.review_for_event = AsyncMock(return_value=expected)

    monkeypatch.setattr(
        "openbot.evaluation.runner.DeepAgentsReviewResponder",
        lambda: mock_responder,
    )

    result = await run_review_sample(
        repo="org/repo",
        pr_number=7,
        pr_diff="--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@ -x\n+y",
    )

    assert result is expected
    mock_responder.review_for_event.assert_called_once()
    call_kwargs = mock_responder.review_for_event.call_args
    event = call_kwargs.args[0]
    assert event.repo == "org/repo"
    assert event.pr_number == 7


@pytest.mark.asyncio
async def test_run_review_sample_passes_diff_to_adapter(monkeypatch) -> None:
    """The EvalChannelAdapter passed to the responder has the correct diff."""
    diff_text = "--- a/x.py\n+++ b/x.py"
    captured_adapter = {}

    async def fake_review(event, *, adapter, run_id=None):
        captured_adapter["adapter"] = adapter
        return ReviewFindings(summary="ok", findings=())

    mock_responder = MagicMock()
    mock_responder.review_for_event = fake_review
    monkeypatch.setattr(
        "openbot.evaluation.runner.DeepAgentsReviewResponder",
        lambda: mock_responder,
    )

    await run_review_sample(repo="org/repo", pr_number=1, pr_diff=diff_text)

    adapter = captured_adapter["adapter"]
    # Verify the adapter exposes the correct diff
    result_diff = await adapter.get_pr_diff(None, 1)
    assert result_diff == diff_text


# ── run_fix_sample ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_fix_sample_calls_responder(monkeypatch) -> None:
    """run_fix_sample passes issue data to the fix responder."""
    attempt = FixAttempt(
        summary="patched",
        files_changed=("foo.py",),
        tests_passed=True,
        test_command="pytest",
        test_output="1 passed",
        diff="diff",
    )
    expected = FixOutcome(attempt=attempt, pr_url=None, error=None)

    mock_responder = MagicMock()
    mock_responder.fix_for_event = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        "openbot.evaluation.runner.DeepAgentsFixResponder",
        lambda: mock_responder,
    )

    fake_sandbox = MagicMock()
    result = await run_fix_sample(
        repo="org/repo",
        issue_number=3,
        issue_title="Bug: crash",
        issue_body="Traceback...",
        base_sha="a" * 40,
        clone_url="https://github.com/org/repo.git",
        sandbox=fake_sandbox,
    )

    assert result is expected
    mock_responder.fix_for_event.assert_called_once()
    call_kwargs = mock_responder.fix_for_event.call_args
    event = call_kwargs.args[0]
    assert event.repo == "org/repo"
    assert event.issue_number == 3


# ── run_chat_sample ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_chat_sample_calls_responder(monkeypatch) -> None:
    """run_chat_sample passes user_request to the chat responder."""
    mock_responder = MagicMock()
    mock_responder.reply_for_event = AsyncMock(return_value="Hi from bot")
    monkeypatch.setattr(
        "openbot.evaluation.runner.DeepAgentsChatResponder",
        lambda: mock_responder,
    )

    result = await run_chat_sample(
        repo="org/repo",
        user_request="What does this repo do?",
    )

    assert result == "Hi from bot"
    mock_responder.reply_for_event.assert_called_once()
    call_kwargs = mock_responder.reply_for_event.call_args
    assert call_kwargs.kwargs["user_request"] == "What does this repo do?"


# ── run_test_generation_sample ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_test_generation_sample_raises_not_implemented() -> None:
    """Stub raises NotImplementedError — v0.1 doesn't support test gen."""
    with pytest.raises(NotImplementedError, match=r"v0\.1"):
        await run_test_generation_sample(
            repo="org/repo",
            pr_number=1,
            pr_diff="",
        )
