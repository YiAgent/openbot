"""Judge model + prompt — eval PRD §10.3 / §17 #5.

The judge LLM scores eval samples. PRD locks two things:

  1. **Model**: default `anthropic/claude-opus-4-7`. Upgrading (e.g. → 4.8)
     requires a `docs/eval/judge-version-log.md` entry (E0-T03 template).

  2. **Prompt version**: every byte of `_JUDGE_PROMPT_BODY` is hashed at import
     time; the hash MUST match `_JUDGE_PROMPT_HASH`. When you edit the prompt,
     bump `JUDGE_PROMPT_VERSION` *and* regenerate `_JUDGE_PROMPT_HASH` (we
     ship a helper assertion: `tests/eval/test_judges.py` lists the fresh
     hash in its failure message).

Why this dance: PRD §10.3 says judge prompt drift is a governance event.
Hash-pinning means an honest mistake (typo, refactor) trips a CI red light
instead of silently rewriting baseline scores.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# eval PRD §17 #5 — default judge model.
DEFAULT_JUDGE_MODEL_ID = "anthropic/claude-opus-4-7"

# eval PRD §10.3 — prompt version. **Bump on every edit to _JUDGE_PROMPT_BODY.**
JUDGE_PROMPT_VERSION: int = 1

# The judge prompt. Keep it minimal in v0.1 — scorer-specific framing lives in
# `evals/scorers/*.py`. This body is a generic "compare candidate to golden,
# return JSON match score".
_JUDGE_PROMPT_BODY: str = """\
You are an evaluation judge. Compare the candidate output against the golden
reference and decide whether they describe the same defect / finding / fix.

Return a single JSON object with these keys:
  - match: boolean         (semantic match, ignoring trivial wording)
  - confidence: float      (0.0-1.0)
  - rationale: string      (one sentence, no chain-of-thought)

Be strict. If the candidate covers only part of the golden, return match=false.
"""

# Hash pin — regenerate when JUDGE_PROMPT_VERSION changes. The test in
# tests/eval/test_judges.py asserts that the live hash matches this constant;
# editing the prompt without updating both this hash AND bumping the version
# fails CI loudly.
_JUDGE_PROMPT_HASH: str = "fa623754502002c32074a3b49a91290a4518de7619441aadb304a220ea834e47"


def _live_hash() -> str:
    return hashlib.sha256(_JUDGE_PROMPT_BODY.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Judge:
    """A versioned (model + prompt) judge handle, immutable per run.

    Construction is cheap; pass it through scorers so every LangSmith run
    records the same (judge_model_id, judge_prompt_version) pair (PRD §10.1).
    """

    model_id: str = DEFAULT_JUDGE_MODEL_ID
    prompt_version: int = JUDGE_PROMPT_VERSION
    prompt_body: str = _JUDGE_PROMPT_BODY

    def fingerprint(self) -> tuple[str, int, str]:
        """`(model_id, prompt_version, prompt_sha256)` — stable across runs."""
        return (
            self.model_id,
            self.prompt_version,
            hashlib.sha256(self.prompt_body.encode("utf-8")).hexdigest(),
        )
