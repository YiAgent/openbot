"""LiteLLM async completion wrapper with built-in cost accounting.

Single entry point for every LLM call OpenBot makes. Every call:

  1. Routes feature → primary model via `openbot.infrastructure.llm.model_router.primary_model_for()`
  2. Calls `litellm.acompletion(...)` — LiteLLM handles vendor fallback
     if the runtime model list is configured for it.
  3. Computes USD cost via `litellm.completion_cost(...)`.
  4. Persists a row to `cost_meter` via `CostMeterRepo.record()` — ALWAYS,
     even when extraction or pricing fails (vendor already billed).
  5. Returns a `CompletionResult` (content + token / cost breakdown).

Budget enforcement (PRD §4.5, lands in the middleware slice) wraps THIS —
it queries `cost_meter` BEFORE allowing the call. This module's responsibility
is to (a) make the call faithfully + (b) record EVERY call honestly, marking
degraded rows so budget logic can distinguish real cheap calls from
"we couldn't price this".

Cost-status semantics (see `CostStatus`):
  - RECORDED        normal call, price + tokens both known
  - PRICED_ZERO     litellm.completion_cost returned None (unknown model)
  - PRICING_FAILED  litellm.completion_cost raised
  - USAGE_MISSING   response had no .usage attribute
  - EXTRACTION_FAILED content extraction raised after vendor billed
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import litellm

from openbot.core.metrics import llm_cost_usd_total
from openbot.infrastructure.llm.model_router import Feature, primary_model_for
from openbot.infrastructure.persistence.models import CostStatus
from openbot.infrastructure.persistence.repository import CostMeterRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """Outcome of one `complete()` call.

    `cost_status` lets the caller see *why* `cost_usd` / tokens might be 0 —
    distinguishing a real cheap call from a degraded record.
    """

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    cost_status: CostStatus

    def __post_init__(self) -> None:
        # Guard rails for invariant the type can't express via fields alone.
        if self.cost_usd < 0:
            raise ValueError(f"cost_usd must be non-negative, got {self.cost_usd}")
        if self.prompt_tokens < 0 or self.completion_tokens < 0:
            raise ValueError(
                f"token counts must be non-negative; got prompt={self.prompt_tokens}, "
                f"completion={self.completion_tokens}"
            )


async def complete(
    feature: Feature,
    messages: list[dict[str, Any]],
    *,
    repo: str,
    task_id: str,
    session: AsyncSession,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    **litellm_kwargs: Any,
) -> CompletionResult:
    """Run one LLM call, ALWAYS record the cost row, return the structured result.

    `session` is **required** — there is no "skip persistence" path. Scripts
    and quick tests must construct a sessionmaker explicitly; silently dropping
    cost rows in production due to a forgotten dependency injection is exactly
    the failure mode budget enforcement cannot survive.

    The flow is wrapped in try/finally so a `cost_meter` row is written even
    when content extraction raises — the vendor has already billed us and we
    must not lose that fact.
    """
    from openbot.core.settings import get_settings

    settings = get_settings()
    model = primary_model_for(feature)

    # Honor custom base URL for Anthropic models (e.g. GLM proxy)
    if "anthropic/" in model and settings.anthropic_api_base:
        litellm_kwargs.setdefault("api_base", settings.anthropic_api_base)

    response = await litellm.acompletion(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        **litellm_kwargs,
    )

    usage, usage_status = _extract_usage(response, model=model, feature=feature)
    cost_usd, cost_status = _extract_cost(response, model=model)

    # `usage_missing` is the more specific (and worse) signal — if usage is
    # gone, cost is necessarily uncertain even if the pricer didn't complain.
    effective_status = usage_status if usage_status is CostStatus.USAGE_MISSING else cost_status

    extraction_error: Exception | None = None
    content = ""
    try:
        content = _extract_content(response)
    except Exception as exc:
        extraction_error = exc
        # Vendor has billed. Force EXTRACTION_FAILED so this row is visible
        # in audit + filtered out of strict budget math.
        effective_status = CostStatus.EXTRACTION_FAILED
        _logger.exception(
            "llm_extraction_failed_after_billing",
            extra={
                "feature": feature.value,
                "model": model,
                "task_id": task_id,
                "cost_usd": str(cost_usd),
            },
        )

    try:
        await CostMeterRepo(session).record(
            repo=repo,
            feature=feature.value,
            task_id=task_id,
            model=model,
            cost_usd=cost_usd,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            cost_status=effective_status,
        )
    except Exception:
        # Persistence itself failed — log loudly. We've still got the call's
        # content; raising here would lose it AND hide the persistence bug.
        _logger.exception(
            "llm_cost_meter_write_failed",
            extra={
                "feature": feature.value,
                "model": model,
                "task_id": task_id,
                "cost_status": effective_status.value,
            },
        )

    # Increment cost counter regardless of Postgres persistence outcome —
    # the metric reflects actual LLM spend, not storage success.
    if effective_status is CostStatus.RECORDED:
        llm_cost_usd_total.labels(feature=feature.value).inc(float(cost_usd))

    if extraction_error is not None:
        raise extraction_error

    return CompletionResult(
        content=content,
        model=model,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        cost_usd=cost_usd,
        cost_status=effective_status,
    )


# ─────────────────────────────────────────────────────────────────────────
# Response extractors — isolated so tests can stub easily and LiteLLM's
# evolving response shape stays in one place. Each returns the value plus
# a CostStatus describing degradation, if any.


def _extract_content(response: Any) -> str:
    """Pull `choices[0].message.content` defensively.

    Returns "" when the content is `None` (e.g. tool-call-only responses
    from Anthropic via LiteLLM). Raises on shape errors so the caller can
    audit-mark the row as EXTRACTION_FAILED.
    """
    try:
        choices = response.choices
        return str(choices[0].message.content or "")
    except (AttributeError, IndexError) as exc:
        raise ValueError(f"LiteLLM response shape missing choices/message: {exc}") from exc


def _extract_usage(
    response: Any, *, model: str, feature: Feature
) -> tuple[dict[str, int], CostStatus]:
    """Pull prompt / completion tokens.

    Returns `({prompt_tokens: 0, completion_tokens: 0}, USAGE_MISSING)`
    with a WARNING log when `response.usage` is missing — this is a signal
    that the vendor returned no token accounting; cost is necessarily
    uncertain. The caller flags the cost_meter row accordingly.

    Also handles the case where `usage` is a `dict` rather than an object
    (older LiteLLM responses) — `getattr` defaults work for both shapes.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        _logger.warning(
            "llm_usage_missing",
            extra={"model": model, "feature": feature.value},
        )
        return {"prompt_tokens": 0, "completion_tokens": 0}, CostStatus.USAGE_MISSING

    # usage may be either an object (newer LiteLLM) or a dict (older).
    def _get(key: str) -> int:
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        return int(value or 0)

    return (
        {"prompt_tokens": _get("prompt_tokens"), "completion_tokens": _get("completion_tokens")},
        CostStatus.RECORDED,
    )


# LiteLLM cost errors we expect — these are "we don't know how to price this"
# signals and SHOULD downgrade to PRICED_ZERO/PRICING_FAILED rather than crash.
# Catching anything broader risks silently zero-ing every call forever if
# LiteLLM's API shape changes (a `Exception` catch would swallow that).
_EXPECTED_PRICING_ERRORS: tuple[type[Exception], ...] = (
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
)
# These names appear under different module paths in different LiteLLM
# versions; check by name at runtime in addition to the stdlib bases above.
_PRICING_ERROR_NAMES: frozenset[str] = frozenset(
    {"NotFoundError", "BadRequestError", "ContentPolicyViolationError"}
)


def _extract_cost(response: Any, *, model: str) -> tuple[Decimal, CostStatus]:
    """Use LiteLLM's `completion_cost` helper.

    Returns `(Decimal("0"), PRICED_ZERO)` when LiteLLM returns `None`
    (unknown-model pricing) and `(Decimal("0"), PRICING_FAILED)` when it
    raises an expected pricing error. We narrow the exception set to known
    "can't price this model" cases — anything else is a real bug that
    SHOULD surface as an exception so it doesn't silently zero out every
    future call.

    The `model` is logged so ops can find unpriced model strings to seed
    LiteLLM's pricing table.
    """
    try:
        cost = litellm.completion_cost(completion_response=response)
    except _EXPECTED_PRICING_ERRORS as exc:
        _logger.error(
            "litellm_cost_calc_failed",
            extra={"model": model, "exc_type": type(exc).__name__, "reason": str(exc)},
        )
        return Decimal("0"), CostStatus.PRICING_FAILED
    except Exception as exc:
        # Named-class fallback for LiteLLM-specific errors not in stdlib bases.
        if type(exc).__name__ in _PRICING_ERROR_NAMES:
            _logger.error(
                "litellm_cost_calc_failed",
                extra={"model": model, "exc_type": type(exc).__name__, "reason": str(exc)},
            )
            return Decimal("0"), CostStatus.PRICING_FAILED
        # Anything else is a real bug — let it propagate. Silent fallback here
        # would hide persistent issues (LiteLLM API drift, etc.) for every call.
        raise

    if cost is None:
        _logger.warning("litellm_cost_unknown_model", extra={"model": model})
        return Decimal("0"), CostStatus.PRICED_ZERO

    decimal_cost = Decimal(str(cost))
    if decimal_cost < 0:
        _logger.error(
            "litellm_negative_cost",
            extra={"model": model, "cost_raw": str(cost)},
        )
        return Decimal("0"), CostStatus.PRICING_FAILED

    return decimal_cost, CostStatus.RECORDED


# ───────────────────────── Port implementation ─────────────────────────

if TYPE_CHECKING:
    from openbot.application.ports.llm import LLMPort


class LiteLLMCompleter:
    """LLMPort impl backed by litellm.acompletion.

    Bypasses the cost-accounting ``complete()`` function above — this
    adapter is the raw LLM call surface for the future agent slice.
    Cost tracking for agent calls will be layered on top separately.
    """

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        response = await litellm.acompletion(**kwargs)
        # Use attribute access — litellm returns a ModelResponse object,
        # and the rest of this module uses the same pattern (e.g. line 181).
        return response.choices[0].message.content


if TYPE_CHECKING:
    _witness: LLMPort = LiteLLMCompleter()
