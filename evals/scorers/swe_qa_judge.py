"""SWE-QA-Pro development judge using the official Appendix D prompt text.

This module intentionally keeps the **official Appendix D prompt body** from
arXiv 2603.16124, but it is not a paper-reproduction scorer:

- the runtime judge is the OpenBot development default
  ``claude-opus-4-7`` rather than the paper's ``GPT-5``;
- the dev scorer makes a single judge call rather than the paper's 3-run
  average.

Those deviations are explicit in :data:`JUDGE_DEVIATIONS` so local evals stay
cheap without being mislabeled as paper-faithful results.

Locked surface (do not edit without bumping ``SWE_QA_JUDGE_VERSION`` and
recording a docs entry under the eval PRD):

  - **Model**: paper Appendix D pins ``GPT-5``; this dev scorer intentionally
    uses ``claude-opus-4-7`` through the shared Anthropic gateway.
  - **Prompt**: one user message containing the official Appendix D prompt text
    end-to-end, including the opening evaluator sentence plus the rubric /
    INPUT / OUTPUT / REQUIREMENT blocks.
  - **No repo/commit injection** into the prompt — the paper's template only
    receives ``question / reference / candidate``.
  - **Single judge call** per (q, a). Paper §4.1 averages 3 runs to reduce
    variance; deferred for cost. Recorded as a known deviation in
    ``JUDGE_DEVIATIONS``.

Table 2 "Overall" column in the paper is the **sum** of the five 1-10 scores
(max 50), not a weighted scalar — verified against the paper's numbers
(e.g. Gemini 2.5 Pro: 2.51+2.13+8.66+8.02+4.16 = 25.48). The §3.2 weighted
form ``r = w·s/10`` is the RL **reward**, not the eval metric; it is still
computed and stashed in metadata for downstream RL comparability.
"""

from __future__ import annotations

from typing import Any

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field

from evals.runtime.config import get_eval_config

# Paper Appendix D, in order.
SWE_QA_DIMENSIONS: tuple[str, ...] = (
    "correctness",
    "completeness",
    "relevance",
    "clarity",
    "reasoning",
)

# Paper §3.2 Eq. (4) — RL reward weights. Surfaced as a derived metric only.
SWE_QA_RL_WEIGHTS: tuple[float, ...] = (0.3, 0.2, 0.2, 0.1, 0.2)
# ─── Known deviations from the paper (arXiv 2603.16124v1) ────────────────────
# Docstring-explained, kept explicit so every run's metadata is self-auditing.
JUDGE_DEVIATIONS: tuple[str, ...] = (
    "judge model is claude-opus-4-7 (paper uses GPT-5)",
    "single judge call per sample (paper averages 3 runs to reduce variance)",
)

# Locked-surface version bump protocol: increment when any judge constant
# (prompt, temperature, max_tokens, structured-output schema) changes and
# record a docs/eval/judge-version-log.md entry.
SWE_QA_JUDGE_VERSION: int = 1

# Paper does not specify temperature / max_tokens. Use the deterministic
# shared defaults from :mod:`evals.runtime.config` (matches every other
# LLM-judge in this repo). Pinned via ``SWE_QA_JUDGE_VERSION`` — see the
# locked-surface note in the module docstring.
#
# Model id resolution (``OPENBOT_SWE_QA_JUDGE_MODEL`` →
# ``OPENBOT_JUDGE_MODEL_ID`` → :data:`config.JUDGE_MODEL_DEFAULT`) flows
# through :class:`JudgeSettings`. Env vars must be **bare** Anthropic ids
# (e.g. ``claude-opus-4-7``); deepagents-style ``anthropic:`` /
# ``anthropic/`` prefixes are no longer stripped.
_judge_settings = get_eval_config().judge
SWE_QA_JUDGE_TEMPERATURE: float = _judge_settings.temperature
SWE_QA_JUDGE_MAX_TOKENS: int = _judge_settings.max_tokens
SWE_QA_JUDGE_MODEL_ID: str = _judge_settings.swe_qa_model_id or _judge_settings.model_id


# ─── Official Appendix D prompt text — DO NOT EDIT ────────────────────────
# Source: arXiv 2603.16124v1 Appendix D, "Prompt Template for LLM-as-Judge".
# The paper shows one continuous prompt block, so we send one user message
# with the same content instead of splitting it into system/user envelopes.

SWE_QA_JUDGE_PROMPT: str = """You are a professional evaluator. Please rate the candidate answer against the reference answer based on five criteria.
Evaluation Criteria and Scoring Guidelines (each scored 1 to 10):
1. Correctness:
10 — Completely correct; core points and details are accurate with no ambiguity.
8-9 — Mostly correct; only minor details are slightly inaccurate or loosely expressed.
6-7 — Partially correct; some errors or omissions, but main points are generally accurate.
4-5 — Several errors or ambiguities that affect understanding of the core information.
2-3 — Many errors; misleading or fails to convey key information.
1 — Serious errors; completely wrong or misleading.
2. Completeness:
10 — Covers all key points from the reference answer without omission.
8-9 — Covers most key points; only minor non-critical information missing.
6-7 — Missing several key points; content is somewhat incomplete.
4-5 — Important information largely missing; content is one-sided.
2-3 — Covers very little relevant information; seriously incomplete.
1 — Covers almost no relevant information; completely incomplete.
3. Relevance:
10 — Content fully focused on the question topic; no irrelevant information.
8-9 — Mostly focused; only minor irrelevant or peripheral information.
6-7 — Generally on topic; some off-topic content but still relevant overall.
4-5 — Topic not sufficiently focused; contains considerable off-topic content.
2-3 — Content deviates from topic; includes excessive irrelevant information.
1 — Majority of content irrelevant to the question.
4. Clarity:
10 — Fluent language; clear and precise expression; very easy to understand.
8-9 — Mostly fluent; clear expression with minor unclear points.
6-7 — Generally clear; some expressions slightly unclear or not concise.
4-5 — Expression somewhat awkward; some ambiguity or lack of fluency.
2-3 — Language obscure; sentences are not smooth; hinders understanding.
1 — Expression confusing; very difficult to understand.
5. Reasoning:
10 — Reasoning is clear, logical, and well-structured; argumentation is excellent.
8-9 — Reasoning is clear and logical; well-structured with solid argumentation.
6-7 — Reasoning generally reasonable; mostly clear logic; minor jumps.
4-5 — Reasoning is average; some logical jumps or organization issues.
2-3 — Reasoning unclear; lacks logical order; difficult to follow.
1 — No clear reasoning; logic is chaotic.
INPUT:
Question:{question}
Reference Answer:{reference}
Candidate Answer:{candidate}
OUTPUT:
Please output ONLY a JSON object with 5 integer fields in the range [1,10], corresponding to the evaluation scores:
{{
"correctness": <1-10>,
"completeness": <1-10>,
"relevance": <1-10>,
"clarity": <1-10>,
"reasoning": <1-10>
}}

REQUIREMENT:
You should assume that a score of 5 represents an average but imperfect answer. Scores above 7 should be reserved for answers that are clearly strong. Do not infer or assume missing information. Score strictly based on what is explicitly stated. No explanation, no extra text, no formatting other than valid JSON"""

# ─── End official prompt block ────────────────────────────────────────────


class SWEQAScorecard(BaseModel):
    """Schema-enforced 5-dimension judge output from the LLM provider."""

    correctness: int = Field(ge=1, le=10, strict=True)
    completeness: int = Field(ge=1, le=10, strict=True)
    relevance: int = Field(ge=1, le=10, strict=True)
    clarity: int = Field(ge=1, le=10, strict=True)
    reasoning: int = Field(ge=1, le=10, strict=True)


def weighted_rl_reward(scores: dict[str, int]) -> float:
    """Paper §3.2 Eq. (4): ``r = w · s / 10``, range [0.1, 1.0].

    For RL training comparability only — not the eval metric.
    """
    return sum(SWE_QA_RL_WEIGHTS[i] * scores[dim] for i, dim in enumerate(SWE_QA_DIMENSIONS)) / 10.0


def overall_score(scores: dict[str, int]) -> int:
    """Paper Table 2 "Overall" column = sum of the five 1-10 dimensions."""
    return sum(scores[d] for d in SWE_QA_DIMENSIONS)


def judge_answer(
    *,
    question: str,
    reference: str,
    candidate: str,
) -> dict[str, Any]:
    """One judge call against the official Appendix D prompt text.

    Returns ``{scores, overall, rl_reward}``:
      - ``scores``: dict of the five 1-10 integer dimensions.
      - ``overall``: paper Table 2 sum (5-50).
      - ``rl_reward``: weighted scalar ``w·s/10`` for RL comparability.
    """
    prompt = SWE_QA_JUDGE_PROMPT.format(
        question=question,
        reference=reference,
        candidate=candidate,
    )
    scorecard = (
        ChatAnthropic(
            model=SWE_QA_JUDGE_MODEL_ID,
            temperature=SWE_QA_JUDGE_TEMPERATURE,
            max_tokens=SWE_QA_JUDGE_MAX_TOKENS,
        )
        .with_structured_output(
            SWEQAScorecard,
            method="json_schema",
        )
        .with_config({"run_name": "swe_qa_pro_judge"})
        .invoke(
            [
                {"role": "user", "content": prompt},
            ]
        )
    )
    scores = {dim: int(getattr(scorecard, dim)) for dim in SWE_QA_DIMENSIONS}
    return {
        "scores": scores,
        "overall": overall_score(scores),
        "rl_reward": weighted_rl_reward(scores),
    }
