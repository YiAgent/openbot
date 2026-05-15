"""LiteLLM async completion wrapper with built-in cost accounting.

Single entry point for every LLM call OpenBot makes. Every call:

  1. Routes feature → primary model via `openbot.llm.router.primary_model_for()`
  2. Calls `litellm.acompletion(...)`; LiteLLM handles vendor fallback if
     the runtime model list is configured for it.
  3. Computes USD cost via `litellm.completion_cost(...)`.
  4. Persists a row to `cost_meter` via `CostMeterRepo.record()`.
  5. Returns a `CompletionResult` (content + token / cost breakdown).

Budget enforcement (PRD §4.5, lands in slice 3/3 middleware) wraps THIS —
it queries `cost_meter` BEFORE allowing the call. So this module's
responsibility is to (a) make the call faithfully + (b) record the cost.
It never enforces a cap itself.

If no session is provided, the call still runs but no cost row is written
(WARNING is logged). This makes the wrapper usable from quick scripts or
tests, while production always wires a session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import litellm

from openbot.llm.router import Feature, primary_model_for
from openbot.persistence.repository import CostMeterRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """Outcome of one `complete()` call."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal


async def complete(
    feature: Feature,
    messages: list[dict[str, Any]],
    *,
    repo: str,
    task_id: str,
    session: AsyncSession | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    **litellm_kwargs: Any,
) -> CompletionResult:
    """Run one LLM call, record the cost, return the structured result.

    Args:
        feature:       PRD §4 feature this call belongs to (drives routing
                       + the row's `feature` column).
        messages:      `[{"role": ..., "content": ...}, ...]` OpenAI-style.
        repo:          repo full_name (e.g. `YiAgent/openbot`) — stored
                       in cost_meter for per-repo budget queries.
        task_id:       webhook delivery_id or composite — stored in cost_meter
                       for per-task budget queries.
        session:       AsyncSession to write the cost row through. If None,
                       call still runs, but cost is NOT persisted (logged).
        max_tokens:    OpenAI-style cap on output tokens.
        temperature:   sampling temperature; default 0.0 = deterministic.
        litellm_kwargs: pass-through to `litellm.acompletion`.
    """
    model = primary_model_for(feature)

    response = await litellm.acompletion(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        **litellm_kwargs,
    )

    content = _extract_content(response)
    usage = _extract_usage(response)
    cost_usd = _extract_cost(response)

    if session is None:
        _logger.warning(
            "llm_complete_no_session",
            extra={
                "feature": feature.value,
                "model": model,
                "task_id": task_id,
                "cost_usd": str(cost_usd),
            },
        )
    else:
        await CostMeterRepo(session).record(
            repo=repo,
            feature=feature.value,
            task_id=task_id,
            model=model,
            cost_usd=cost_usd,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
        )

    return CompletionResult(
        content=content,
        model=model,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        cost_usd=cost_usd,
    )


# ─────────────────────────────────────────────────────────────────────────
# Response extractors — isolated so tests can stub easily and so LiteLLM's
# evolving response shape stays in one place.


def _extract_content(response: Any) -> str:
    """Pull `choices[0].message.content` defensively."""
    try:
        choices = response.choices
        return str(choices[0].message.content or "")
    except (AttributeError, IndexError) as exc:
        raise ValueError(f"LiteLLM response shape missing choices/message: {exc}") from exc


def _extract_usage(response: Any) -> dict[str, int]:
    """Pull prompt / completion tokens. Both default to 0 if absent."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0}
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


def _extract_cost(response: Any) -> Decimal:
    """Use LiteLLM's `completion_cost` helper. Returns Decimal for cost_meter Numeric column."""
    try:
        cost = litellm.completion_cost(completion_response=response)
    except Exception:
        # Cost calc can fail for unknown models. Don't let that abort the
        # caller — log + record zero. BudgetEnforcement will treat that as
        # "no spend recorded", which is the conservative direction.
        _logger.exception("litellm_cost_calc_failed")
        return Decimal("0")
    return Decimal(str(cost))
