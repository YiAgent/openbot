# tests/application/dispatcher/test_classifier.py
"""Unit tests: classifier.py — output parsing and stages_from_classifier."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openbot.dispatcher.classifier import (
    ChatClassifierOutput,
    ReviewClassifierOutput,
    TriageClassifierOutput,
    classify_event,
    stages_from_classifier,
)
from openbot.domain.workflows import Feature

# ── stages_from_classifier — pure, no I/O ────────────────────────────────


def test_triage_bug_with_repro_includes_reproduce_stage() -> None:
    out = TriageClassifierOutput(
        type="bug",
        severity_guess="high",
        has_reproduction_info=True,
        looks_like_spam=False,
    )
    assert stages_from_classifier(Feature.TRIAGE, out) == [
        "classify_labels",
        "reproduce",
        "summarize",
    ]


def test_triage_feature_no_reproduce_stage() -> None:
    out = TriageClassifierOutput(
        type="feature",
        severity_guess="medium",
        has_reproduction_info=False,
        looks_like_spam=False,
    )
    stages = stages_from_classifier(Feature.TRIAGE, out)
    assert "reproduce" not in stages
    assert "classify_labels" in stages and "summarize" in stages


def test_review_uses_suggested_subagents() -> None:
    out = ReviewClassifierOutput(
        change_size_class="l",
        touches_security_paths=True,
        is_breaking=False,
        suggested_subagents=("correctness", "security"),
    )
    assert stages_from_classifier(Feature.REVIEW, out) == ["correctness", "security"]


def test_review_empty_suggested_falls_back_to_correctness() -> None:
    out = ReviewClassifierOutput(
        change_size_class="xs",
        touches_security_paths=False,
        is_breaking=False,
        suggested_subagents=(),
    )
    assert stages_from_classifier(Feature.REVIEW, out) == ["correctness"]


def test_chat_readonly_qa() -> None:
    out = ChatClassifierOutput(intent="readonly_qa", needs_clarification=False, scope_hint=None)
    assert stages_from_classifier(Feature.CHAT, out) == ["readonly_qa"]


def test_chat_unclear_returns_empty_list() -> None:
    out = ChatClassifierOutput(intent="unclear", needs_clarification=True, scope_hint=None)
    assert stages_from_classifier(Feature.CHAT, out) == []


def test_none_output_returns_empty_list() -> None:
    """classifier_skipped path: None → [] (worker runs all stages)."""
    for feature in (Feature.TRIAGE, Feature.REVIEW, Feature.CHAT):
        assert stages_from_classifier(feature, None) == []


def test_fix_with_none_returns_empty() -> None:
    assert stages_from_classifier(Feature.FIX, None) == []


def test_fix_with_output_returns_full_pipeline() -> None:
    """FIX with any output (shouldn't happen in practice) → full pipeline."""
    # classify_event always returns None for FIX, but stages_from_classifier
    # can be called directly — verify the FIX branch returns the full pipeline.
    out = TriageClassifierOutput(
        type="bug",
        severity_guess="high",
        has_reproduction_info=False,
        looks_like_spam=False,
    )
    # FIX branch doesn't use the output type, so any non-None works
    assert stages_from_classifier(Feature.FIX, out) == ["plan", "read", "patch", "test", "self_fix"]


# ── classify_event — I/O mocked ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_triage_success() -> None:
    """Happy path: LLM returns valid JSON → TriageClassifierOutput."""
    response = MagicMock()
    response.choices[0].message.content = json.dumps(
        {
            "type": "bug",
            "severity_guess": "high",
            "has_reproduction_info": True,
            "looks_like_spam": False,
        }
    )
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        result = await classify_event(feature=Feature.TRIAGE, body="crash on submit", redis=None)

    assert isinstance(result, TriageClassifierOutput)
    assert result.type == "bug"
    assert result.has_reproduction_info is True


@pytest.mark.asyncio
async def test_classify_llm_exception_returns_none() -> None:
    """LLM exception → None (fail-open)."""
    with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=RuntimeError("down")):
        result = await classify_event(feature=Feature.TRIAGE, body="anything", redis=None)
    assert result is None


@pytest.mark.asyncio
async def test_classify_invalid_json_returns_none() -> None:
    """Non-JSON LLM response → None (fail-open)."""
    response = MagicMock()
    response.choices[0].message.content = "Sorry, I cannot help."
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        result = await classify_event(feature=Feature.TRIAGE, body="test", redis=None)
    assert result is None


@pytest.mark.asyncio
async def test_classify_review_happy_path() -> None:
    response = MagicMock()
    response.choices[0].message.content = json.dumps(
        {
            "change_size_class": "l",
            "touches_security_paths": True,
            "is_breaking": False,
            "suggested_subagents": ["correctness", "security"],
        }
    )
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        result = await classify_event(feature=Feature.REVIEW, body="auth refactor", redis=None)

    assert isinstance(result, ReviewClassifierOutput)
    assert result.touches_security_paths is True
    assert "security" in result.suggested_subagents


@pytest.mark.asyncio
async def test_classify_redis_cache_hit_skips_llm() -> None:
    """Cache hit → LLM not called."""
    cached = {
        "type": "feature",
        "severity_guess": "low",
        "has_reproduction_info": False,
        "looks_like_spam": False,
    }
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=json.dumps(cached).encode())

    with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
        result = await classify_event(feature=Feature.TRIAGE, body="dark mode", redis=mock_redis)

    mock_llm.assert_not_called()
    assert isinstance(result, TriageClassifierOutput)
    assert result.type == "feature"


@pytest.mark.asyncio
async def test_classify_redis_miss_calls_llm_and_stores() -> None:
    """Cache miss → LLM called, result stored in Redis."""
    response = MagicMock()
    payload = {
        "type": "question",
        "severity_guess": "low",
        "has_reproduction_info": False,
        "looks_like_spam": False,
    }
    response.choices[0].message.content = json.dumps(payload)

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)  # cache miss
    mock_redis.setex = AsyncMock()

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        result = await classify_event(feature=Feature.TRIAGE, body="how do I?", redis=mock_redis)

    mock_redis.setex.assert_called_once()
    assert isinstance(result, TriageClassifierOutput)
    assert result.type == "question"


@pytest.mark.asyncio
async def test_classify_fix_returns_none() -> None:
    """FIX feature has no D10 classifier in v0.1."""
    result = await classify_event(feature=Feature.FIX, body="fix it", redis=None)
    assert result is None


# ── _strip_markdown_fences ────────────────────────────────────────────────────


def test_strip_markdown_fences_plain_json() -> None:
    """Plain JSON (no fences) is returned unchanged."""
    from openbot.dispatcher.classifier import _strip_markdown_fences

    raw = '{"type": "bug"}'
    assert _strip_markdown_fences(raw) == raw


def test_strip_markdown_fences_json_fence() -> None:
    """```json ... ``` fences are stripped."""
    from openbot.dispatcher.classifier import _strip_markdown_fences

    raw = '```json\n{"type": "bug"}\n```'
    assert _strip_markdown_fences(raw) == '{"type": "bug"}'


def test_strip_markdown_fences_plain_fence() -> None:
    """``` ... ``` (no language tag) fences are stripped."""
    from openbot.dispatcher.classifier import _strip_markdown_fences

    raw = '```\n{"type": "bug"}\n```'
    assert _strip_markdown_fences(raw) == '{"type": "bug"}'


def test_strip_markdown_fences_multiline() -> None:
    """Multi-line JSON inside fences is fully extracted."""
    from openbot.dispatcher.classifier import _strip_markdown_fences

    raw = '```json\n{\n  "change_size_class": "xs",\n  "is_breaking": false\n}\n```'
    result = _strip_markdown_fences(raw)
    assert result.startswith("{")
    assert '"change_size_class"' in result
    assert "```" not in result


@pytest.mark.asyncio
async def test_classify_review_strips_markdown_fences(monkeypatch) -> None:
    """Classifier parses GLM-style ```json ... ``` wrapped review response."""
    import json as _json

    from openbot.dispatcher.classifier import ReviewClassifierOutput, classify_event
    from openbot.domain.workflows import Feature

    payload = {
        "change_size_class": "xs",
        "touches_security_paths": False,
        "is_breaking": False,
        "suggested_subagents": ["correctness"],
    }
    response = MagicMock()
    # Simulate GLM-5.1 wrapping JSON in markdown fences
    response.choices[0].message.content = f"```json\n{_json.dumps(payload)}\n```"

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        result = await classify_event(feature=Feature.REVIEW, body="small docs change", redis=None)

    assert isinstance(result, ReviewClassifierOutput)
    assert result.change_size_class == "xs"
    assert result.is_breaking is False
