# openbot/dispatcher/classifier.py
"""D10 LLM classifier — one-shot claude-sonnet-4-6 with Redis TTL cache.

Fail-open: any exception returns None so callers set classifier_skipped=True.
Uses litellm directly (not the complete() wrapper) — no DB session required.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from openbot.domain.workflows import Feature

if TYPE_CHECKING:
    import redis.asyncio as redis_async

_logger = logging.getLogger(__name__)
_CACHE_TTL: int = 3600  # seconds
_CLASSIFIER_VERSION: str = "v1"


@dataclass(frozen=True, slots=True)
class TriageClassifierOutput:
    type: str  # "bug" | "feature" | "question" | "spam" | "other"
    severity_guess: str  # "critical" | "high" | "medium" | "low"
    has_reproduction_info: bool
    looks_like_spam: bool


@dataclass(frozen=True, slots=True)
class ReviewClassifierOutput:
    change_size_class: str  # "xs" | "s" | "m" | "l" | "xl"
    touches_security_paths: bool
    is_breaking: bool
    suggested_subagents: tuple[str, ...]  # subset of correctness/security/arch/docs/tests


@dataclass(frozen=True, slots=True)
class ChatClassifierOutput:
    intent: str  # "readonly_qa" | "draft_pr" | "unclear" | "out_of_scope"
    needs_clarification: bool
    scope_hint: str | None


ClassifierOutput = TriageClassifierOutput | ReviewClassifierOutput | ChatClassifierOutput


def _cache_key(feature: Feature, body: str) -> str:
    digest = hashlib.sha256(
        # Truncate to 2000 chars: classification is dominated by the preamble;
        # long bodies with identical first 2000 chars share a cache key intentionally.
        f"{feature.value}|{body[:2000]}|{_CLASSIFIER_VERSION}".encode()
    ).hexdigest()[:32]
    return f"openbot:classifier:{feature.value}:{digest}"


async def _get_cached(redis: redis_async.Redis, key: str) -> dict[str, Any] | None:
    try:
        value = await redis.get(key)
        if value is not None:
            return json.loads(value)  # type: ignore[no-any-return]
    except Exception:
        pass
    return None


async def _set_cached(redis: redis_async.Redis, key: str, data: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        await redis.setex(key, _CACHE_TTL, json.dumps(data, ensure_ascii=False))


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
    # Feature.CHAT
    return (
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
            tuple(str(s) for s in raw_agents) if isinstance(raw_agents, list) else ("correctness",)
        )
        return ReviewClassifierOutput(
            change_size_class=str(data.get("change_size_class", "m")),
            touches_security_paths=bool(data.get("touches_security_paths", False)),
            is_breaking=bool(data.get("is_breaking", False)),
            suggested_subagents=agents,
        )
    # Feature.CHAT
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
        # Deferred import: fail-open if litellm is not installed.
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
        triage = cast(TriageClassifierOutput, output)
        stages = ["classify_labels", "summarize"]
        if triage.has_reproduction_info and triage.type == "bug":
            stages.insert(1, "reproduce")
        return stages

    if feature is Feature.REVIEW:
        review = cast(ReviewClassifierOutput, output)
        return list(review.suggested_subagents) if review.suggested_subagents else ["correctness"]

    if feature is Feature.CHAT:
        chat = cast(ChatClassifierOutput, output)
        return ["readonly_qa"] if chat.intent == "readonly_qa" else []

    if feature is Feature.FIX:
        return ["plan", "read", "patch", "test", "self_fix"]

    return []
