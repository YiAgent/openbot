# openbot/dispatcher/classifier.py
"""One-shot LLM classifier — claude-sonnet-4-6 with Redis TTL cache.

Fail-open: any exception returns None so callers set classifier_skipped=True.
Uses litellm directly (not the complete() wrapper) — no DB session required.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from openbot.core.metrics import classifier_error_total
from openbot.dispatcher.context import extract_event_context
from openbot.domain.workflows import Feature

if TYPE_CHECKING:
    import redis.asyncio as redis_async

    from openbot.domain.events import UnifiedEvent

_logger = logging.getLogger(__name__)
_CACHE_TTL: Final[int] = 3600  # seconds
_CLASSIFIER_VERSION: Final[str] = "v1"
_LLM_TIMEOUT_S: Final[float] = 10.0
_BODY_MAX_CHARS: Final[int] = 2000

TriageType = Literal["bug", "feature", "question", "spam", "other"]
SeverityGuess = Literal["critical", "high", "medium", "low"]
ChangeSizeClass = Literal["xs", "s", "m", "l", "xl"]
ChatIntent = Literal["readonly_qa", "draft_pr", "unclear", "out_of_scope"]


@dataclass(frozen=True, slots=True)
class TriageClassifierOutput:
    type: TriageType
    severity_guess: SeverityGuess
    has_reproduction_info: bool
    looks_like_spam: bool


@dataclass(frozen=True, slots=True)
class ReviewClassifierOutput:
    change_size_class: ChangeSizeClass
    touches_security_paths: bool
    is_breaking: bool
    suggested_subagents: tuple[str, ...]  # subset of correctness/security/arch/docs/tests


@dataclass(frozen=True, slots=True)
class ChatClassifierOutput:
    intent: ChatIntent
    needs_clarification: bool
    scope_hint: str | None


ClassifierOutput = TriageClassifierOutput | ReviewClassifierOutput | ChatClassifierOutput


def _cache_key(feature: Feature, body: str) -> str:
    # Truncate body: classification is dominated by the preamble; long bodies
    # with identical leading chars share a cache key intentionally.
    digest = hashlib.sha256(
        f"{feature.value}|{body[:_BODY_MAX_CHARS]}|{_CLASSIFIER_VERSION}".encode()
    ).hexdigest()[:32]
    return f"openbot:classifier:{feature.value}:{digest}"


async def _get_cached(redis: redis_async.Redis, key: str) -> dict[str, Any] | None:
    try:
        value = await redis.get(key)
        if value is not None:
            return json.loads(value)  # type: ignore[no-any-return]
    except Exception:
        _logger.warning("classifier_cache_get_failed", extra={"key": key}, exc_info=True)
    return None


async def _set_cached(redis: redis_async.Redis, key: str, data: dict[str, Any]) -> None:
    try:
        await redis.setex(key, _CACHE_TTL, json.dumps(data, ensure_ascii=False))
    except Exception:
        _logger.warning("classifier_cache_set_failed", extra={"key": key}, exc_info=True)


_TRIAGE_TYPES: Final[frozenset[str]] = frozenset(("bug", "feature", "question", "spam", "other"))
_SEVERITY_VALUES: Final[frozenset[str]] = frozenset(("critical", "high", "medium", "low"))
_CHANGE_SIZE_VALUES: Final[frozenset[str]] = frozenset(("xs", "s", "m", "l", "xl"))
_CHAT_INTENTS: Final[frozenset[str]] = frozenset(
    ("readonly_qa", "draft_pr", "unclear", "out_of_scope")
)


def _build_prompt(feature: Feature, body: str) -> str:
    body = body[:_BODY_MAX_CHARS]
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


def parse_classifier_output(feature: Feature, data: dict[str, Any]) -> ClassifierOutput:
    """Reverse-deserialize a classifier output dict into the typed dataclass.

    Public symmetric inverse of ``dataclasses.asdict`` over a
    ``ClassifierOutput``. The worker uses it to rehydrate the
    ``TaskSpec.classifier_output`` payload (a plain dict, since
    ``TaskSpec`` is serialised across the Redis Stream boundary) back
    into the typed union the policy gate consumes via ``isinstance``.

    The function is intentionally permissive: unknown enum-like
    values fall back to safe defaults (``"other"``, ``"medium"``,
    ``"m"``, ``"unclear"``) rather than raising — this is the same
    forgiveness the LLM path uses on a malformed model response.
    """
    if feature is Feature.TRIAGE:
        triage_type = data.get("type")
        severity = data.get("severity_guess")
        return TriageClassifierOutput(
            type=cast("TriageType", triage_type) if triage_type in _TRIAGE_TYPES else "other",
            severity_guess=(
                cast("SeverityGuess", severity) if severity in _SEVERITY_VALUES else "medium"
            ),
            has_reproduction_info=bool(data.get("has_reproduction_info", False)),
            looks_like_spam=bool(data.get("looks_like_spam", False)),
        )
    if feature is Feature.REVIEW:
        raw_agents = data.get("suggested_subagents", ["correctness"])
        agents: tuple[str, ...] = (
            tuple(str(s) for s in raw_agents) if isinstance(raw_agents, list) else ("correctness",)
        )
        size = data.get("change_size_class")
        return ReviewClassifierOutput(
            change_size_class=(
                cast("ChangeSizeClass", size) if size in _CHANGE_SIZE_VALUES else "m"
            ),
            touches_security_paths=bool(data.get("touches_security_paths", False)),
            is_breaking=bool(data.get("is_breaking", False)),
            suggested_subagents=agents,
        )
    # Feature.CHAT
    intent = data.get("intent")
    scope = data.get("scope_hint")
    return ChatClassifierOutput(
        intent=cast("ChatIntent", intent) if intent in _CHAT_INTENTS else "unclear",
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
                return parse_classifier_output(feature, cached)
            except Exception:
                # Corrupt cache entry; delete so a fresh result repopulates it.
                _logger.warning("classifier_cache_corrupt", extra={"key": key}, exc_info=True)
                with contextlib.suppress(Exception):
                    await redis.delete(key)

    try:
        # Deferred import: fail-open if litellm is not installed.
        import litellm

        response = await litellm.acompletion(
            model="anthropic/claude-sonnet-4-6",
            messages=[{"role": "user", "content": _build_prompt(feature, body)}],
            max_tokens=500,
            temperature=0,
            timeout=_LLM_TIMEOUT_S,
        )
        content: str = response.choices[0].message.content or ""
        data: dict[str, Any] = json.loads(content.strip())
        result = parse_classifier_output(feature, data)
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


async def classify_for_dispatch(
    *,
    event: UnifiedEvent,
    feature: Feature,
    redis: redis_async.Redis | None,
) -> ClassifierOutput | None:
    """Single-source classifier hook shared by all dispatcher entry points.

    All three sibling entry points (``decide_and_enqueue``, ``run_dispatch``,
    ``execute_handler``) used to either inline the classify call or skip it
    entirely; that drift meant the worker path occasionally bypassed
    classification and the static-only ``SandboxPolicy`` was over-eager.
    Centralising the four responsibilities here keeps them lock-step:

      1. **Feature gate** — FIX has no lightweight classifier in v0.1; skip
         the LLM round-trip rather than spending a token budget for a None.
      2. **Body extraction** — ``extract_event_context`` is the pure path
         from ``UnifiedEvent.raw`` to the comment/body string the classifier
         actually looks at. Centralised so the three call sites can't drift.
      3. **Fail-open** — any exception (litellm crash, Redis timeout,
         malformed JSON) returns None. ``derive_sandbox_policy`` treats None
         as "respect the static SandboxPolicy", so the workflow still runs.
      4. **Logging** — emits ``classifier_exception_in_dispatch`` once at
         WARN level so ops can spot a regressing classifier without paging.
    """
    if feature is Feature.FIX:
        return None
    try:
        ev_ctx = extract_event_context(event)
        return await classify_event(
            feature=feature,
            body=ev_ctx.classification_body,
            redis=redis,
        )
    except Exception:
        _logger.exception(
            "classifier_exception_in_dispatch",
            extra={"delivery_id": event.delivery_id, "repo": event.repo},
        )
        # ``classifier_error_total{feature}`` is the only metric signal
        # that the classifier is regressing — the fail-open return below
        # means the dispatcher itself never raises, so a buried WARN log
        # is otherwise the only trace. Looked up via ``sys.modules`` so
        # tests can monkeypatch the module attribute.
        import sys as _sys

        _shim = _sys.modules.get("openbot.dispatcher.classifier")
        _counter = (
            getattr(_shim, "classifier_error_total", None) if _shim is not None else None
        ) or classifier_error_total
        _counter.labels(feature=feature.value).inc()
        return None
