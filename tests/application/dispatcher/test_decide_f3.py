# tests/application/dispatcher/test_decide_f3.py
"""Integration tests: decide_and_enqueue() D10 classifier wiring (F3)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openbot.dispatcher import decide_and_enqueue
from openbot.domain.events import EventKind, UnifiedEvent
from tests._fakes.channel_adapter import FakeChannelAdapter
from tests._fakes.config_loader import FakeConfigLoader
from tests._fakes.queue import FakeQueue


def _issue_event(body: str) -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="del-f3-issue",
        kind=EventKind.ISSUE_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        issue_number=9,
        pr_number=None,
        installation_id=100,
        comment_body=None,
        raw={"issue": {"number": 9, "body": body, "labels": []}},
    )


def _pr_event(head_sha: str = "SHA-head", before: str | None = None) -> UnifiedEvent:
    raw: dict = {
        "pull_request": {
            "number": 42,
            "additions": 10,
            "deletions": 5,
            "changed_files": 2,
            "head": {"sha": head_sha},
            "base": {"sha": "SHA-base"},
        }
    }
    if before is not None:
        raw["before"] = before
    return UnifiedEvent(
        channel="github",
        delivery_id="del-f3-pr",
        kind=EventKind.PR_OPENED,
        repo="org/repo",
        actor="alice",
        actor_type="User",
        issue_number=None,
        pr_number=42,
        installation_id=100,
        comment_body=None,
        raw=raw,
    )


@pytest.mark.asyncio
async def test_classifier_output_stored_in_task_spec() -> None:
    """Classifier result is serialised into TaskSpec.classifier_output."""
    from openbot.application.router import dispatch_for

    event = _issue_event("This is a bug: app crashes when clicking submit.")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    classifier_data = {
        "type": "bug",
        "severity_guess": "high",
        "has_reproduction_info": True,
        "looks_like_spam": False,
    }
    response = MagicMock()
    response.choices[0].message.content = json.dumps(classifier_data)

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event,
            dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue,
            session_factory=None,
            redis=None,
        )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.classifier_output == classifier_data
    assert spec.classifier_skipped is False


@pytest.mark.asyncio
async def test_classifier_failure_is_fail_open() -> None:
    """LLM exception → classifier_skipped=True, spec still enqueued."""
    from openbot.application.router import dispatch_for

    event = _issue_event("App crashes on login.")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=RuntimeError("down")):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event,
            dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue,
            session_factory=None,
            redis=None,
        )

    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.classifier_output is None
    assert spec.classifier_skipped is True


@pytest.mark.asyncio
async def test_stages_to_run_populated_from_classifier() -> None:
    """stages_to_run reflects classifier output (bug with repro → includes reproduce)."""
    from openbot.application.router import dispatch_for

    event = _issue_event("Bug: crash with full stack trace attached.")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    response = MagicMock()
    response.choices[0].message.content = json.dumps(
        {
            "type": "bug",
            "severity_guess": "high",
            "has_reproduction_info": True,
            "looks_like_spam": False,
        }
    )

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event,
            dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue,
            session_factory=None,
            redis=None,
        )

    spec = queue.task_specs[0]
    assert "reproduce" in spec.stages_to_run
    assert "classify_labels" in spec.stages_to_run


@pytest.mark.asyncio
async def test_pr_event_gets_incremental_fields() -> None:
    """PR event → DiffScope computed, is_incremental/is_force_push stored in spec."""
    from openbot.application.router import dispatch_for

    event = _pr_event(head_sha="SHA-new", before="SHA-old")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    response = MagicMock()
    response.choices[0].message.content = json.dumps(
        {
            "change_size_class": "s",
            "touches_security_paths": False,
            "is_breaking": False,
            "suggested_subagents": ["correctness"],
        }
    )

    queue = FakeQueue()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event,
            dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=queue,
            session_factory=None,
            redis=None,
        )

    # With last_reviewed_sha=None (v0.1 default) → first review, not incremental
    assert len(queue.task_specs) == 1
    spec = queue.task_specs[0]
    assert spec.is_force_push is False


@pytest.mark.asyncio
async def test_decide_and_enqueue_still_never_raises() -> None:
    """decide_and_enqueue must swallow all exceptions including classifier errors."""
    from openbot.application.router import dispatch_for

    event = _issue_event("Something.")
    dispatch = dispatch_for(event)
    assert dispatch is not None

    with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=Exception("boom")):
        # Should not raise
        await decide_and_enqueue(
            adapter=FakeChannelAdapter(),
            event=event,
            dispatch=dispatch,
            config_loader=FakeConfigLoader(),
            queue=FakeQueue(),
            session_factory=None,
            redis=None,
        )
