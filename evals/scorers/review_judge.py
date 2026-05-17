"""Martian-CRB LLM judge — verbatim from `withmartian/code-review-benchmark`.

This judge mirrors the upstream Martian Code Review Benchmark
(`offline/code_review_benchmark/step3_judge_comments.py`) and the open-swe
reviewer eval (`evals/reviewer/judge.py`) so OpenBot's review numbers are
directly comparable to martian's published Devin Review numbers and to
open-swe's reviewer baseline.

Locked surface (do not edit without bumping `MARTIAN_JUDGE_VERSION` and
recording a `docs/eval/judge-version-log.md` entry):

  - Temperature:    0.0
  - max_tokens:     4096 (Anthropic requires a cap; upstream OpenAI call
                    leaves max_tokens unset — the verdict JSON is tiny)
  - System message: ``MARTIAN_JUDGE_SYSTEM_PROMPT``, sent as its own
                    ``{"role": "system"}`` message (matches upstream)
  - User message:   ``MARTIAN_JUDGE_PROMPT`` with raw ``golden_comment``
                    and ``candidate`` substitutions — no extra
                    ``Severity:`` / ``Location:`` headers

Model id (formerly hardcoded to ``claude-opus-4-5``) is now resolved via
:func:`evals.common.judge_client.resolve_judge_model`, which reads
``OPENBOT_REVIEW_JUDGE_MODEL_ID`` first, then the shared
``OPENBOT_JUDGE_MODEL_ID``, then a fallback. This is required because the
configured Anthropic-compatible gateway (``ANTHROPIC_BASE_URL``) routinely
ships a curated subset of models — pinning a model the gateway doesn't
expose makes every dev / smoke run crash with 400. The byte-identical
prompt + temperature + max_tokens are still the load-bearing parts of the
martian-CRB contract; the model id is recorded in the experiment
metadata so historical runs are still reproducible.

Upstream source: https://github.com/withmartian/code-review-benchmark
(MIT licensed; commit 807d469 pinned by
`evals/scripts/build_review_martian_dataset.py`).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from evals.common import config
from evals.common.config import get_eval_config
from evals.common.judge_client import get_judge_client, resolve_judge_model

logger = logging.getLogger(__name__)

# v4: realigned the prompt surface byte-for-byte with upstream martian-CRB
# ``step3_judge_comments.py``:
#   * system / user messages are now sent as two separate messages (upstream
#     uses ``{"role": "system", ...}`` + ``{"role": "user", ...}``); v3 was
#     concatenating them into a single user message, which subtly shifted
#     role framing for the model.
#   * golden / candidate placeholders now receive the raw comment string
#     (matching upstream ``JUDGE_PROMPT.format(golden_comment=gc["comment"],
#     candidate=candidate)``); v3 wrapped them in open-swe-style
#     ``Severity:`` / ``Location:`` / ``Comment:`` headers, which is a
#     different prompt and produced different match decisions.
# v3 had moved the model id to :func:`resolve_judge_model`; that resolution
# is unchanged. Temperature and max_tokens are also unchanged.
MARTIAN_JUDGE_VERSION: int = 4
MARTIAN_JUDGE_MODEL_ID: str = resolve_judge_model(
    per_judge_env=config.REVIEW_JUDGE_MODEL_ENV,
)
# Temperature + max_tokens are part of v4's locked surface (martian-CRB
# contract). They happen to match the shared judge defaults in
# :mod:`evals.common.config`; if those defaults ever drift, this constant
# must stay pinned to 0.0 / 4096 until ``MARTIAN_JUDGE_VERSION`` is bumped.
# Upstream OpenAI call does not set ``max_tokens``; ``ChatAnthropic``
# requires one, and 4096 is well above the short JSON verdict.
MARTIAN_JUDGE_TEMPERATURE: float = get_eval_config().judge.temperature
MARTIAN_JUDGE_MAX_TOKENS: int = get_eval_config().judge.max_tokens

# Byte-identical to upstream ``step3_judge_comments.py`` constants. Do not
# edit either without bumping ``MARTIAN_JUDGE_VERSION`` and logging a
# ``docs/eval/judge-version-log.md`` entry.
MARTIAN_JUDGE_SYSTEM_PROMPT: str = (
    "You are a precise code review evaluator. Always respond with valid JSON."
)
MARTIAN_JUDGE_PROMPT: str = """You are evaluating AI code review tools.
Determine if the candidate issue matches the golden (expected) comment.

Golden Comment (the issue we're looking for):
{golden_comment}

Candidate Issue (from the tool's review):
{candidate}

Instructions:
- Determine if the candidate identifies the SAME underlying issue as the golden comment
- Accept semantic matches - different wording is fine if it's the same problem
- Focus on whether they point to the same bug, concern, or code issue

Respond with ONLY a JSON object:
{{"reasoning": "brief explanation", "match": true/false, "confidence": 0.0-1.0}}"""


class MartianVerdict(BaseModel):
    """Schema-enforced judge reply.

    Field names mirror martian's published ``step3_judge_comments.py`` exactly
    (``reasoning`` rather than ``rationale``) so the byte-identical prompt
    contract from the module docstring stays intact. The OpenBot-shaped
    rename to ``rationale`` happens in :func:`judge_verdict` below.
    """

    reasoning: str = Field(..., description="One-sentence justification for the verdict.")
    match: bool = Field(..., description="True iff candidate identifies the same issue.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Calibrated [0,1] confidence.")


def _get_client():  # type: ignore[no-untyped-def]
    """Shared ``ChatAnthropic`` via :func:`evals.common.judge_client.get_judge_client`."""
    return get_judge_client(
        model_id=MARTIAN_JUDGE_MODEL_ID,
        temperature=MARTIAN_JUDGE_TEMPERATURE,
        max_tokens=MARTIAN_JUDGE_MAX_TOKENS,
    )


def format_golden(golden: dict[str, Any]) -> str:
    """Render a golden finding as the raw ``{golden_comment}`` substitution.

    Upstream ``step3_judge_comments.py`` substitutes ``gc["comment"]``
    directly into the prompt — no ``Severity:`` / ``Comment:`` headers.
    The earlier open-swe-style wrapping subtly biased the judge, so v4
    drops it. ``body`` is preferred over ``comment`` for forward-compat
    with payloads that use ``body`` (e.g. GitHub review-comment dicts).
    """
    return str(golden.get("body") or golden.get("comment") or "")


def format_candidate(candidate: dict[str, Any]) -> str:
    """Render a candidate finding as the raw ``{candidate}`` substitution.

    Upstream feeds the candidate's text directly (one of the strings
    returned by ``get_candidates``). We accept dicts here for backward
    compatibility with OpenBot's review-overlap pipeline, but emit only
    the comment body — no ``Location:`` / ``Severity:`` prefix.
    """
    return str(candidate.get("body") or candidate.get("comment") or "")


def judge_pair(golden: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Run one (golden, candidate) match. Returns the raw judge JSON.

    Result shape (per martian step3_judge_comments.py):
      {"reasoning": str, "match": bool, "confidence": float}

    Structured-output strategy: bind :class:`MartianVerdict` via langchain's
    ``with_structured_output(method="json_schema")`` so the provider either
    emits a schema-valid object or langchain raises.
    """
    prompt = MARTIAN_JUDGE_PROMPT.format(
        golden_comment=format_golden(golden),
        candidate=format_candidate(candidate),
    )
    messages = [
        {"role": "system", "content": MARTIAN_JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    verdict = (
        _get_client()
        .with_structured_output(MartianVerdict, method="json_schema")
        .with_config({"run_name": "martian_review_judge"})
        .invoke(messages)
    )
    return {
        "reasoning": verdict.reasoning,
        "match": bool(verdict.match),
        "confidence": float(verdict.confidence),
    }


def judge_verdict(golden: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """`JudgeFn`-compatible wrapper: returns the OpenBot `JudgeVerdict` shape.

    OpenBot's `evals.scorers.review_overlap.JudgeFn` expects keys
    ``{match, confidence, rationale}``; martian returns ``reasoning``. This
    shim renames the field so the existing overlap math (greedy set-cover)
    runs unchanged.
    """
    res = judge_pair(golden, candidate)
    return {
        "match": bool(res.get("match", False)),
        "confidence": float(res.get("confidence", 0.0)),
        "rationale": str(res.get("reasoning", "")),
    }
