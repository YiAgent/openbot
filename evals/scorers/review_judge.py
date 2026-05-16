"""Martian-CRB LLM judge — verbatim from `withmartian/code-review-benchmark`.

This judge mirrors the upstream Martian Code Review Benchmark
(`offline/code_review_benchmark/step3_judge_comments.py`) and the open-swe
reviewer eval (`evals/reviewer/judge.py`) so OpenBot's review numbers are
directly comparable to martian's published Devin Review numbers and to
open-swe's reviewer baseline.

Locked surface (do not edit without bumping `MARTIAN_JUDGE_VERSION` and
recording a `docs/eval/judge-version-log.md` entry):

  - Model:        `claude-opus-4-5`  (martian's evaluator model)
  - Temperature:  0.0
  - max_tokens:   512
  - System msg:   "You are a precise code review evaluator. Always respond with valid JSON."
  - User prompt:  byte-identical to martian's `JUDGE_PROMPT`

Unlike `evals.common.judges.Judge`, this judge is *not* hash-pinned against
PRD §17 #5 because it intentionally diverges from the OpenBot default judge
(claude-opus-4-7) to stay byte-identical with martian/open-swe. The judge
is namespaced (`martian_*`) to make the divergence loud at call sites.

Upstream source: https://github.com/withmartian/code-review-benchmark
(MIT licensed; commit 807d469 pinned by
`evals/scripts/build_review_martian_dataset.py`).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_anthropic import ChatAnthropic

MARTIAN_JUDGE_VERSION: int = 1
MARTIAN_JUDGE_MODEL_ID: str = "claude-opus-4-5"
MARTIAN_JUDGE_TEMPERATURE: float = 0.0
MARTIAN_JUDGE_MAX_TOKENS: int = 512

MARTIAN_JUDGE_SYSTEM: str = (
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


_client: ChatAnthropic | None = None


def _get_client() -> ChatAnthropic:
    """Lazy singleton — avoids constructing a client at import time."""
    global _client
    if _client is None:
        _client = ChatAnthropic(
            model=MARTIAN_JUDGE_MODEL_ID,
            temperature=MARTIAN_JUDGE_TEMPERATURE,
            max_tokens=MARTIAN_JUDGE_MAX_TOKENS,
        )
    return _client


def format_golden(golden: dict[str, Any]) -> str:
    """Render a golden finding for the judge prompt.

    Mirrors open-swe `evals/reviewer/judge.py::_format_golden` so candidate /
    golden framing is identical across both projects.
    """
    parts: list[str] = []
    if golden.get("severity"):
        parts.append(f"Severity: {golden['severity']}")
    parts.append(f"Comment: {golden.get('body') or golden.get('comment', '')}")
    return "\n".join(parts)


def format_candidate(candidate: dict[str, Any]) -> str:
    """Render a candidate finding for the judge prompt.

    Mirrors open-swe `evals/reviewer/judge.py::_format_candidate`.
    """
    parts: list[str] = []
    if candidate.get("file"):
        loc = candidate["file"]
        if candidate.get("line") is not None:
            loc += f":{candidate['line']}"
        parts.append(f"Location: {loc}")
    if candidate.get("severity"):
        parts.append(f"Severity: {candidate['severity']}")
    parts.append(f"Comment: {candidate.get('body') or candidate.get('comment') or ''}")
    return "\n".join(parts)


def _parse_judge_reply(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of the model reply; tolerate code fences."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return {"match": False, "confidence": 0.0, "reasoning": f"unparseable: {raw[:200]}"}
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {"match": False, "confidence": 0.0, "reasoning": f"unparseable: {raw[:200]}"}


def judge_pair(golden: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Run one (golden, candidate) match. Returns the raw judge JSON.

    Result shape (per martian step3_judge_comments.py):
      {"reasoning": str, "match": bool, "confidence": float}
    """
    prompt = MARTIAN_JUDGE_PROMPT.format(
        golden_comment=format_golden(golden),
        candidate=format_candidate(candidate),
    )
    msg = _get_client().invoke(
        [
            {"role": "system", "content": MARTIAN_JUDGE_SYSTEM},
            {"role": "user", "content": prompt},
        ]
    )
    raw = msg.content if isinstance(msg.content, str) else str(msg.content)
    return _parse_judge_reply(raw)


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
