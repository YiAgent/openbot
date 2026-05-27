"""Tests for issue dedup use case."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from openbot.application.use_cases.dedup import (
    DedupCandidate,
    check_duplicates,
    render_dedup_comment,
)
from openbot.domain.events import EventKind, UnifiedEvent


def _make_event() -> UnifiedEvent:
    return UnifiedEvent(
        channel="github",
        delivery_id="test-dedup",
        kind=EventKind.ISSUE_OPENED,
        repo="owner/repo",
        actor="test-user",
        issue_number=100,
    )


# ── render_dedup_comment ─────────────────────────────────────────────────


def test_render_empty_candidates():
    assert render_dedup_comment([]) == ""


def test_render_single_candidate():
    candidates = [DedupCandidate(issue_number=42, title="Bug in login", similarity=0.85)]
    comment = render_dedup_comment(candidates)
    assert "#42" in comment
    assert "Bug in login" in comment
    assert "85%" in comment
    assert "duplicate" in comment.lower()


def test_render_multiple_candidates():
    candidates = [
        DedupCandidate(issue_number=10, title="Issue A", similarity=0.90),
        DedupCandidate(issue_number=20, title="Issue B", similarity=0.80),
        DedupCandidate(issue_number=30, title="Issue C", similarity=0.76),
    ]
    comment = render_dedup_comment(candidates)
    assert "#10" in comment
    assert "#20" in comment
    assert "#30" in comment
    assert "90%" in comment
    assert "80%" in comment
    assert "76%" in comment


# ── check_duplicates ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_duplicates_not_configured():
    event = _make_event()
    adapter = AsyncMock()
    result = await check_duplicates(event, adapter, issue_title="Test", issue_body="Body")
    assert result.candidates == []
    assert result.comment == ""


@pytest.mark.asyncio
async def test_check_duplicates_no_results():
    event = _make_event()
    adapter = AsyncMock()
    embed_func = AsyncMock(return_value=[0.1, 0.2, 0.3])
    search_func = AsyncMock(return_value=[])

    result = await check_duplicates(
        event,
        adapter,
        issue_title="Test",
        issue_body="Body",
        embedding_func=embed_func,
        search_func=search_func,
    )
    assert result.candidates == []
    assert result.comment == ""


@pytest.mark.asyncio
async def test_check_duplicates_with_results():
    event = _make_event()
    adapter = AsyncMock()
    embed_func = AsyncMock(return_value=[0.1, 0.2, 0.3])
    search_func = AsyncMock(
        return_value=[
            {"issue_number": 42, "title": "Similar issue", "similarity": 0.85},
            {"issue_number": 99, "title": "Another one", "similarity": 0.78},
        ]
    )

    result = await check_duplicates(
        event,
        adapter,
        issue_title="New bug",
        issue_body="Something broke",
        embedding_func=embed_func,
        search_func=search_func,
    )
    assert len(result.candidates) == 2
    assert result.candidates[0].issue_number == 42
    assert result.candidates[0].similarity == 0.85
    assert "#42" in result.comment
    assert "#99" in result.comment


@pytest.mark.asyncio
async def test_check_duplicates_handles_embedding_error():
    event = _make_event()
    adapter = AsyncMock()
    embed_func = AsyncMock(side_effect=Exception("API error"))

    result = await check_duplicates(
        event,
        adapter,
        issue_title="Test",
        issue_body="Body",
        embedding_func=embed_func,
    )
    assert result.candidates == []
    assert result.comment == ""
