"""Unit tests for EvalChannelAdapter.

Verifies:
  - reply() captures messages instead of posting to GitHub
  - create_pr_review() captures reviews
  - create_branch() raises EvalSideEffectError
  - open_pull_request() raises EvalSideEffectError
  - get_pr_diff() returns the pre-set diff
  - read_file() returns text from pre-set file map
  - grep_repo() returns matches from pre-set content
"""

from __future__ import annotations

import pytest

from openbot.domain.events import EventKind, UnifiedEvent
from openbot.evaluation.adapters import EvalChannelAdapter, EvalSideEffectError


def _event(**overrides) -> UnifiedEvent:
    defaults: dict = {
        "channel": "eval",
        "delivery_id": "d-1",
        "kind": EventKind.PR_OPENED,
        "repo": "org/repo",
        "actor": "alice",
        "pr_number": 42,
    }
    defaults.update(overrides)
    return UnifiedEvent(**defaults)


@pytest.mark.asyncio
async def test_reply_captures_message() -> None:
    adapter = EvalChannelAdapter(pr_diff="diff --git a/f.py ...")
    event = _event()
    result = await adapter.reply(event, "hello from bot")
    assert result["ok"] is True
    assert adapter.replies == [(event.resource_key, "hello from bot")]


@pytest.mark.asyncio
async def test_create_pr_review_captures_review() -> None:
    adapter = EvalChannelAdapter(pr_diff="")
    event = _event()
    result = await adapter.create_pr_review(event, 42, body="LGTM", event_type="APPROVE")
    assert result["ok"] is True
    assert len(adapter.pr_reviews) == 1
    assert adapter.pr_reviews[0]["body"] == "LGTM"
    assert adapter.pr_reviews[0]["event_type"] == "APPROVE"


@pytest.mark.asyncio
async def test_create_branch_raises_side_effect_error() -> None:
    adapter = EvalChannelAdapter(pr_diff="")
    event = _event()
    with pytest.raises(EvalSideEffectError, match="create_branch"):
        await adapter.create_branch(event, "feat/x", "deadbeef" * 5)


@pytest.mark.asyncio
async def test_open_pull_request_raises_side_effect_error() -> None:
    adapter = EvalChannelAdapter(pr_diff="")
    event = _event()
    with pytest.raises(EvalSideEffectError, match="open_pull_request"):
        await adapter.open_pull_request(event, title="t", body="b", head="feat/x", base="main")


@pytest.mark.asyncio
async def test_get_pr_diff_returns_preset_diff() -> None:
    diff_text = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@ -x\n+y"
    adapter = EvalChannelAdapter(pr_diff=diff_text)
    result = await adapter.get_pr_diff(_event(), 42)
    assert result == diff_text


@pytest.mark.asyncio
async def test_read_file_returns_preset_content() -> None:
    adapter = EvalChannelAdapter(pr_diff="", files={"README.md": "# Hello"})
    result = await adapter.read_file(_event(), "README.md")
    assert result == "# Hello"


@pytest.mark.asyncio
async def test_read_file_returns_empty_for_unknown() -> None:
    adapter = EvalChannelAdapter(pr_diff="")
    result = await adapter.read_file(_event(), "nonexistent.py")
    assert result == ""


@pytest.mark.asyncio
async def test_grep_repo_returns_matches_from_files() -> None:
    adapter = EvalChannelAdapter(
        pr_diff="",
        files={"main.py": "def foo():\n    pass\n", "utils.py": "def bar():\n    pass\n"},
    )
    matches = await adapter.grep_repo(_event(), pattern="def ")
    assert any("main.py" in m for m in matches)
    assert any("utils.py" in m for m in matches)


@pytest.mark.asyncio
async def test_get_issue_returns_preset_issue() -> None:
    adapter = EvalChannelAdapter(
        pr_diff="",
        issue={"title": "Bug report", "body": "It crashes", "base_sha": "abc"},
    )
    result = await adapter.get_issue(_event(), 1)
    assert result["title"] == "Bug report"
