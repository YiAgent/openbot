# Webhook-Worker Layering F3 — Plan (Part 2: Task 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Context:** Part 1 added the TaskSpec F3 fields and `incremental.py`. This part creates `classifier.py` — the D10 LLM module. Part 3 wires everything into `decide_and_enqueue()`.

**Tech Stack:** Python 3.12, pytest-asyncio, litellm (direct, not the `complete()` wrapper), redis.asyncio, `unittest.mock`.

---

### Task 3: Create `openbot/dispatcher/classifier.py`

**Files:**
- Create: `openbot/dispatcher/classifier.py`
- Test: `tests/application/dispatcher/test_classifier.py` (new)

One `classify_event()` async function: checks Redis cache, falls back to one `litellm.acompletion` call. Returns typed frozen dataclass on success, `None` on any failure (fail-open). `stages_from_classifier()` is a pure function that maps typed output → `list[str]`.

- [ ] **Step 1: Write failing tests**

Create `tests/application/dispatcher/test_classifier.py`:

```python
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
        type="bug", severity_guess="high",
        has_reproduction_info=True, looks_like_spam=False,
    )
    assert stages_from_classifier(Feature.TRIAGE, out) == [
        "classify_labels", "reproduce", "summarize"
    ]


def test_triage_feature_no_reproduce_stage() -> None:
    out = TriageClassifierOutput(
        type="feature", severity_guess="medium",
        has_reproduction_info=False, looks_like_spam=False,
    )
    stages = stages_from_classifier(Feature.TRIAGE, out)
    assert "reproduce" not in stages
    assert "classify_labels" in stages and "summarize" in stages


def test_review_uses_suggested_subagents() -> None:
    out = ReviewClassifierOutput(
        change_size_class="l", touches_security_paths=True,
        is_breaking=False, suggested_subagents=("correctness", "security"),
    )
    assert stages_from_classifier(Feature.REVIEW, out) == ["correctness", "security"]


def test_review_empty_suggested_falls_back_to_correctness() -> None:
    out = ReviewClassifierOutput(
        change_size_class="xs", touches_security_paths=False,
        is_breaking=False, suggested_subagents=(),
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


# ── classify_event — I/O mocked ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_classify_triage_success() -> None:
    """Happy path: LLM returns valid JSON → TriageClassifierOutput."""
    response = MagicMock()
    response.choices[0].message.content = json.dumps({
        "type": "bug", "severity_guess": "high",
        "has_reproduction_info": True, "looks_like_spam": False,
    })
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
    response.choices[0].message.content = json.dumps({
        "change_size_class": "l", "touches_security_paths": True,
        "is_breaking": False, "suggested_subagents": ["correctness", "security"],
    })
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
        result = await classify_event(feature=Feature.REVIEW, body="auth refactor", redis=None)

    assert isinstance(result, ReviewClassifierOutput)
    assert result.touches_security_paths is True
    assert "security" in result.suggested_subagents


@pytest.mark.asyncio
async def test_classify_redis_cache_hit_skips_llm() -> None:
    """Cache hit → LLM not called."""
    cached = {
        "type": "feature", "severity_guess": "low",
        "has_reproduction_info": False, "looks_like_spam": False,
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
        "type": "question", "severity_guess": "low",
        "has_reproduction_info": False, "looks_like_spam": False,
    }
    response.choices[0].message.content = json.dumps(payload)

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)   # cache miss
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
```

- [ ] **Step 2: Run to confirm FAIL**

```bash
cd /Users/wy/projects/openbot/.worktrees/feat-webhook-worker-layering-f2
python -m pytest tests/application/dispatcher/test_classifier.py -v 2>&1 | tail -20
```

Expected: `ModuleNotFoundError: openbot.dispatcher.classifier`.

- [ ] **Step 3: Create `openbot/dispatcher/classifier.py`**

```python
# openbot/dispatcher/classifier.py
"""D10 LLM classifier — one-shot claude-sonnet-4-6 with Redis TTL cache.

Fail-open: any exception returns None so callers set classifier_skipped=True.
Uses litellm directly (not the complete() wrapper) — no DB session required.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openbot.domain.workflows import Feature

if TYPE_CHECKING:
    import redis.asyncio as redis_async

_logger = logging.getLogger(__name__)
_CACHE_TTL: int = 3600        # seconds
_CLASSIFIER_VERSION: str = "v1"


@dataclass(frozen=True, slots=True)
class TriageClassifierOutput:
    type: str             # "bug" | "feature" | "question" | "spam" | "other"
    severity_guess: str   # "critical" | "high" | "medium" | "low"
    has_reproduction_info: bool
    looks_like_spam: bool


@dataclass(frozen=True, slots=True)
class ReviewClassifierOutput:
    change_size_class: str           # "xs" | "s" | "m" | "l" | "xl"
    touches_security_paths: bool
    is_breaking: bool
    suggested_subagents: tuple[str, ...]  # subset of correctness/security/arch/docs/tests


@dataclass(frozen=True, slots=True)
class ChatClassifierOutput:
    intent: str            # "readonly_qa" | "draft_pr" | "unclear" | "out_of_scope"
    needs_clarification: bool
    scope_hint: str | None


ClassifierOutput = TriageClassifierOutput | ReviewClassifierOutput | ChatClassifierOutput


def _cache_key(feature: Feature, body: str) -> str:
    digest = hashlib.sha256(
        f"{feature.value}|{body[:2000]}|{_CLASSIFIER_VERSION}".encode()
    ).hexdigest()[:32]
    return f"openbot:classifier:{feature.value}:{digest}"


async def _get_cached(redis: redis_async.Redis, key: str) -> dict[str, Any] | None:
    try:
        value = await redis.get(key)
        if value is not None:
            return json.loads(value)
    except Exception:
        pass
    return None


async def _set_cached(redis: redis_async.Redis, key: str, data: dict[str, Any]) -> None:
    try:
        await redis.setex(key, _CACHE_TTL, json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


def _build_prompt(feature: Feature, body: str) -> str:
    body = body[:2000]
    if feature is Feature.TRIAGE:
        return (
            "Classify this GitHub issue. Reply with valid JSON only, no markdown:\n"
            '{"type":"bug"|"feature"|"question"|"spam"|"other",'
            '"severity_guess":"critical"|"high"|"medium"|"low",'
            '"has_reproduction_info":true|false,'
            f'"looks_like_spam":true|false}}\n\nIssue body:\n{body}'
        )
    if feature is Feature.REVIEW:
        return (
            "Classify this PR. Reply with valid JSON only, no markdown:\n"
            '{"change_size_class":"xs"|"s"|"m"|"l"|"xl",'
            '"touches_security_paths":true|false,'
            '"is_breaking":true|false,'
            '"suggested_subagents":["correctness"] or subset of '
            '["correctness","security","arch","docs","tests"]}'
            f"\n\nPR body:\n{body}"
        )
    return (  # Feature.CHAT
        "Classify this @mention. Reply with valid JSON only, no markdown:\n"
        '{"intent":"readonly_qa"|"draft_pr"|"unclear"|"out_of_scope",'
        '"needs_clarification":true|false,'
        '"scope_hint":null or short string}'
        f"\n\nMention body:\n{body}"
    )


def _parse_output(feature: Feature, data: dict[str, Any]) -> ClassifierOutput:
    if feature is Feature.TRIAGE:
        return TriageClassifierOutput(
            type=str(data.get("type", "other")),
            severity_guess=str(data.get("severity_guess", "medium")),
            has_reproduction_info=bool(data.get("has_reproduction_info", False)),
            looks_like_spam=bool(data.get("looks_like_spam", False)),
        )
    if feature is Feature.REVIEW:
        raw_agents = data.get("suggested_subagents", ["correctness"])
        agents: tuple[str, ...] = (
            tuple(str(s) for s in raw_agents)
            if isinstance(raw_agents, list) else ("correctness",)
        )
        return ReviewClassifierOutput(
            change_size_class=str(data.get("change_size_class", "m")),
            touches_security_paths=bool(data.get("touches_security_paths", False)),
            is_breaking=bool(data.get("is_breaking", False)),
            suggested_subagents=agents,
        )
    scope = data.get("scope_hint")
    return ChatClassifierOutput(
        intent=str(data.get("intent", "unclear")),
        needs_clarification=bool(data.get("needs_clarification", True)),
        scope_hint=str(scope) if isinstance(scope, str) else None,
    )


async def classify_event(
    *,
    feature: Feature,
    body: str,
    redis: redis_async.Redis | None,
) -> ClassifierOutput | None:
    """Run one-shot LLM classification with Redis caching.

    Returns typed output on success, None on any failure (fail-open).
    """
    if feature not in (Feature.TRIAGE, Feature.REVIEW, Feature.CHAT):
        return None  # FIX has no lightweight classifier in v0.1

    key = _cache_key(feature, body)

    if redis is not None:
        cached = await _get_cached(redis, key)
        if cached is not None:
            try:
                return _parse_output(feature, cached)
            except Exception:
                pass  # corrupt cache entry; fall through to LLM

    try:
        import litellm

        response = await litellm.acompletion(
            model="anthropic/claude-sonnet-4-6",
            messages=[{"role": "user", "content": _build_prompt(feature, body)}],
            max_tokens=500,
            temperature=0,
        )
        content: str = response.choices[0].message.content or ""
        data: dict[str, Any] = json.loads(content.strip())
        result = _parse_output(feature, data)
        if redis is not None:
            await _set_cached(redis, key, data)
        return result
    except Exception:
        _logger.exception(
            "classifier_failed",
            extra={"feature": feature.value, "body_len": len(body)},
        )
        return None


def stages_from_classifier(
    feature: Feature,
    output: ClassifierOutput | None,
) -> list[str]:
    """Map classifier output to stages_to_run list.

    Empty list means the worker runs all stages (classifier_skipped path).
    """
    if output is None:
        return []

    if feature is Feature.TRIAGE:
        assert isinstance(output, TriageClassifierOutput)
        stages = ["classify_labels", "summarize"]
        if output.has_reproduction_info and output.type == "bug":
            stages.insert(1, "reproduce")
        return stages

    if feature is Feature.REVIEW:
        assert isinstance(output, ReviewClassifierOutput)
        return list(output.suggested_subagents) if output.suggested_subagents else ["correctness"]

    if feature is Feature.CHAT:
        assert isinstance(output, ChatClassifierOutput)
        return ["readonly_qa"] if output.intent == "readonly_qa" else []

    if feature is Feature.FIX:
        return ["plan", "read", "patch", "test", "self_fix"]

    return []
```

- [ ] **Step 4: Run tests to confirm PASS**

```bash
python -m pytest tests/application/dispatcher/test_classifier.py -v 2>&1 | tail -20
```

Expected: all 11 tests pass.

- [ ] **Step 5: Run full dispatcher suite**

```bash
python -m pytest tests/application/dispatcher/ -v 2>&1 | tail -20
```

- [ ] **Step 6: Commit**

```bash
git add openbot/dispatcher/classifier.py tests/application/dispatcher/test_classifier.py
git commit -m "feat(dispatcher): add classifier.py — D10 LLM classifier with Redis cache (F3)"
```

---

**Continue with Part 3** (`2026-05-20-webhook-worker-layering-f3-part3.md`) for Tasks 4–5 (wiring into `decide_and_enqueue()` + acceptance verification).
