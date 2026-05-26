"""Unit tests for openbot.evaluation.runner.

Tests use monkeypatching to avoid real LLM calls. The runner's job
is to wire the correct event + adapter, not to test the responders.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from openbot.domain.checkout import CloneStrategy
from openbot.domain.fix import FixAttempt, FixOutcome
from openbot.domain.repro import ReproOutcome, ReproStatus
from openbot.domain.review import ReviewFindings
from openbot.evaluation.runner import (
    run_chat_sample,
    run_fix_sample,
    run_review_sample,
    run_test_generation_sample,
)

# ── run_review_sample ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
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

    async def fake_review(event, *, adapter, run_id=None, **_kwargs):
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
    """run_fix_sample opens sandbox, clones, then calls the fix responder."""
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

    # Build a factory that yields a mock sandbox — simulating what
    # build_sandbox_factory(settings) returns in production.
    fake_sandbox = AsyncMock()
    fake_sandbox.clone = AsyncMock()

    @asynccontextmanager
    async def fake_factory():
        yield fake_sandbox

    result = await run_fix_sample(
        repo="org/repo",
        issue_number=3,
        issue_title="Bug: crash",
        issue_body="Traceback...",
        base_sha="a" * 40,
        clone_url="https://github.com/org/repo.git",
        sandbox_factory=fake_factory,
    )

    assert result is expected

    # Verify clone was called with correct args before the agent ran.
    fake_sandbox.clone.assert_awaited_once_with(
        repo_url="https://github.com/org/repo.git",
        ref="a" * 40,
        token="",
        strategy=CloneStrategy.BLOBLESS,
    )

    # Verify the responder received the correct event.
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
async def test_run_test_generation_sample_calls_responder(monkeypatch) -> None:
    """run_test_generation_sample opens sandbox, clones, then calls the repro responder."""
    expected = ReproOutcome(
        status=ReproStatus.REPRODUCED,
        summary="Test reproduces the bug.",
        command="python -m pytest tests/test_repro_42.py -v",
        exit_code=1,
        output_excerpt="FAILED tests/test_repro_42.py::test_bug",
        hypothesis="Off-by-one in parser",
        repro_artifact="diff --git a/tests/test_repro_42.py ...",
        repro_artifact_filename="tests/test_repro_42.py",
    )

    mock_responder = MagicMock()
    mock_responder.reproduce_for_event = AsyncMock(return_value=expected)
    monkeypatch.setattr(
        "openbot.evaluation.runner.DeepAgentsReproResponder",
        lambda: mock_responder,
    )

    fake_sandbox = AsyncMock()
    fake_sandbox.clone = AsyncMock()

    @asynccontextmanager
    async def fake_factory():
        yield fake_sandbox

    result = await run_test_generation_sample(
        instance_id="org__repo-42",
        repo="org/repo",
        issue_number=42,
        problem_statement="Parser crashes on empty input.",
        base_sha="b" * 40,
        clone_url="https://github.com/org/repo.git",
        sandbox_factory=fake_factory,
    )

    assert result is expected

    # Verify the sandbox was cloned at the correct ref before the agent ran.
    fake_sandbox.clone.assert_awaited_once_with(
        repo_url="https://github.com/org/repo.git",
        ref="b" * 40,
        token="",
        strategy=CloneStrategy.BLOBLESS,
    )

    # Verify the repro responder received the correct event.
    mock_responder.reproduce_for_event.assert_called_once()
    call_kwargs = mock_responder.reproduce_for_event.call_args
    event = call_kwargs.args[0]
    assert event.repo == "org/repo"
    assert event.issue_number == 42
