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
    _read_build_requires,
    _setup_repo_env,
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
    # _setup_repo_env reads pyproject.toml then calls sandbox.run.
    # Return a minimal pyproject.toml with build-system.requires so the
    # build-deps install step is exercised.
    fake_sandbox.read_file = AsyncMock(
        return_value=(
            "[build-system]\n"
            'requires = ["setuptools>=42", "wheel", "cython==0.29.22"]\n'
            'build-backend = "setuptools.build_meta"\n'
        ),
    )
    fake_sandbox.run = AsyncMock(return_value=MagicMock(exit_code=0))

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

    # Verify _setup_repo_env ran:
    # 1. read_file("pyproject.toml") to extract build-system.requires
    fake_sandbox.read_file.assert_awaited_once_with("pyproject.toml")
    # 2. pip install build deps (setuptools, wheel, cython — version pins stripped)
    fake_sandbox.run.assert_awaited()
    build_deps_cmd = fake_sandbox.run.call_args_list[0].kwargs["command"]
    assert build_deps_cmd[:3] == ["pip", "install", "--quiet"]
    assert "setuptools" in build_deps_cmd
    assert "wheel" in build_deps_cmd
    assert "cython" in build_deps_cmd
    # Version specifiers should be stripped — no ">=42" or "==0.29.22"
    for dep in build_deps_cmd[3:]:
        assert ">=" not in dep and "==" not in dep
    # 3. editable install with --no-build-isolation
    editable_install_cmd = fake_sandbox.run.call_args_list[1].kwargs["command"]
    assert editable_install_cmd == [
        "pip",
        "install",
        "--quiet",
        "-e",
        ".[test]",
        "--no-build-isolation",
    ]

    # Verify the repro responder received the correct event.
    mock_responder.reproduce_for_event.assert_called_once()
    call_kwargs = mock_responder.reproduce_for_event.call_args
    event = call_kwargs.args[0]
    assert event.repo == "org/repo"
    assert event.issue_number == 42


# ── _read_build_requires ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_build_requires_strips_version_pinning() -> None:
    """Version specifiers and env markers are stripped; bare names returned."""
    toml = (
        "[build-system]\n"
        "requires = [\n"
        '    "setuptools>=42",\n'
        '    "wheel",\n'
        '    "cython==0.29.22",\n'
        '    "numpy>=1.19,<2.0",\n'
        '    "jinja2>=2.10; python_version > \\"3.6\\"",\n'
        "]\n"
        'build-backend = "setuptools.build_meta"\n'
    )
    sandbox = AsyncMock()
    sandbox.read_file = AsyncMock(return_value=toml)

    deps = await _read_build_requires(sandbox)

    assert deps == ["setuptools", "wheel", "cython", "numpy"]
    # jinja2 is skipped because it has an env marker (; python_version ...)


@pytest.mark.asyncio
async def test_read_build_requires_empty_when_no_pyproject() -> None:
    """Returns [] when pyproject.toml is missing or empty."""
    for raw in ("", "  ", None):
        sandbox = AsyncMock()
        sandbox.read_file = AsyncMock(return_value=raw)
        assert await _read_build_requires(sandbox) == []


@pytest.mark.asyncio
async def test_read_build_requires_empty_when_no_build_system() -> None:
    """Returns [] when pyproject.toml has no [build-system] section."""
    toml = '[project]\nname = "foo"\nversion = "1.0"\n'
    sandbox = AsyncMock()
    sandbox.read_file = AsyncMock(return_value=toml)
    assert await _read_build_requires(sandbox) == []


@pytest.mark.asyncio
async def test_read_build_requires_handles_read_error() -> None:
    """Returns [] when read_file raises (e.g. sandbox file not found)."""
    sandbox = AsyncMock()
    sandbox.read_file = AsyncMock(side_effect=FileNotFoundError)
    assert await _read_build_requires(sandbox) == []


# ── _setup_repo_env ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_repo_env_skips_build_deps_when_no_pyproject() -> None:
    """When pyproject.toml is missing, _setup_repo_env skips build-deps and
    goes straight to editable install."""
    sandbox = AsyncMock()
    sandbox.read_file = AsyncMock(return_value="")
    sandbox.run = AsyncMock(return_value=MagicMock(exit_code=0))

    await _setup_repo_env(sandbox)

    # Only one run call: the editable install (no build-deps step)
    assert sandbox.run.call_count == 1
    cmd = sandbox.run.call_args_list[0].kwargs["command"]
    assert "--no-build-isolation" in cmd


@pytest.mark.asyncio
async def test_setup_repo_env_falls_back_to_pytest_on_all_failures() -> None:
    """When all editable installs fail, falls back to pip install pytest."""
    sandbox = AsyncMock()
    sandbox.read_file = AsyncMock(return_value="")
    sandbox.run = AsyncMock(return_value=MagicMock(exit_code=1))

    await _setup_repo_env(sandbox)

    # Four calls: .[test], .[tests], .[testing] all fail → fallback pytest
    # (no build-deps since pyproject.toml is empty)
    assert sandbox.run.call_count == 4
    last_cmd = sandbox.run.call_args_list[3].kwargs["command"]
    assert last_cmd == ["pip", "install", "--quiet", "pytest"]
