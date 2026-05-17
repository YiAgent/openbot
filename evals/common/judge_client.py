"""Shared Anthropic chat client for LLM-as-judge scorers.

Every judge in this repo (Martian-CRB ``review_judge``, development
``swe_qa_judge``, future judges) goes through this module so:

- One client construction site → one place to wire up retries, base-URL
  overrides (``ANTHROPIC_BASE_URL`` for self-hosted / proxy gateways like
  xiaomi's Anthropic-compatible endpoint), and version pins.
- Same caching policy: clients are cached by ``(model, temperature, max_tokens)``
  so a long-running eval reuses one HTTP connection pool per judge config.
- Same provider: ``ChatAnthropic`` — SWE-QA-Pro's local development scorer
  intentionally stays on the configured Anthropic gateway even though the
  paper pins GPT-5. Per-judge modules record that divergence in their
  ``JUDGE_DEVIATIONS`` (or equivalent) constant so it's loud and auditable.

Model id normalisation
----------------------
``ChatAnthropic`` expects a bare model id (``claude-opus-4-7``). If callers
pass ``anthropic:claude-opus-4-7`` or ``anthropic/claude-opus-4-7`` (the
deepagents / Inspect AI conventions respectively), the prefix is stripped
silently so the same env-var string can flow through agents and judges
without surprises.
"""

from __future__ import annotations

import os
from functools import lru_cache

from langchain_anthropic import ChatAnthropic

from evals.common import config

# ─── Shared judge model resolution ────────────────────────────────────────
#
# Every LLM-as-judge scorer in this repo (martian-CRB review_judge,
# SWE-QA-Pro swe_qa_judge, future judges) routes through the *same*
# Anthropic-compatible gateway configured by ``ANTHROPIC_BASE_URL``.
# Hardcoding a per-judge model id (e.g. "claude-opus-4-5") makes the
# judge break the moment the gateway doesn't ship that exact model — and
# proxies routinely expose a curated subset.
#
# Resolution order (first non-empty wins):
#   1. The judge module's own override env var (e.g.
#      :data:`config.REVIEW_JUDGE_MODEL_ENV` /
#      :data:`config.SWE_QA_JUDGE_MODEL_ENV`) — useful for A/B experiments
#      where one judge runs on a different model than the rest.
#   2. :data:`config.JUDGE_MODEL_ENV` — the shared cross-judge default.
#   3. :data:`config.JUDGE_MODEL_DEFAULT` — last-resort hardcoded
#      fallback so the module imports cleanly in environments with no
#      Doppler / no env (CI lint, unit tests, IDE).


def resolve_judge_model(*, per_judge_env: str | None = None) -> str:
    """Resolve the judge model id for one scorer.

    Args:
        per_judge_env: Optional name of a judge-specific override env var
            (e.g. :data:`config.SWE_QA_JUDGE_MODEL_ENV`). When set and
            non-empty, it wins over the shared
            :data:`config.JUDGE_MODEL_ENV` default.

    Returns:
        ``provider:model`` (or bare model) id ready to feed into
        :func:`get_judge_client`. Caller does not need to strip the
        ``anthropic:`` prefix — the client factory does that.
    """
    if per_judge_env:
        override = os.environ.get(per_judge_env)
        if override:
            return override
    shared = os.environ.get(config.JUDGE_MODEL_ENV)
    if shared:
        return shared
    return config.JUDGE_MODEL_DEFAULT


def _strip_anthropic_prefix(model_id: str) -> str:
    """Drop ``anthropic:`` / ``anthropic/`` prefixes for ``ChatAnthropic``."""
    for prefix in ("anthropic:", "anthropic/"):
        if model_id.startswith(prefix):
            return model_id[len(prefix) :]
    return model_id


@lru_cache(maxsize=16)
def get_judge_client(
    *,
    model_id: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
) -> ChatAnthropic:
    """Lazy, process-cached ``ChatAnthropic`` for an LLM-as-judge call.

    Args:
        model_id: Anthropic model id, with or without the ``anthropic:`` /
            ``anthropic/`` prefix. Routes through ``ANTHROPIC_BASE_URL`` when
            set, so the same call works against api.anthropic.com or a
            self-hosted Anthropic-compatible gateway.
        temperature: Defaults to ``0.0`` — every judge in this repo is
            deterministic by design.
        max_tokens: Defaults to ``512`` — judges emit a short JSON object,
            not an essay. Bump only after auditing whether longer replies
            are actually needed.

    Returns:
        A ``ChatAnthropic`` instance cached by ``(model, temperature,
        max_tokens)``. Safe to call from multiple threads — the LangChain
        client is thread-safe for ``.invoke``.
    """
    return ChatAnthropic(
        model=_strip_anthropic_prefix(model_id),
        temperature=temperature,
        max_tokens=max_tokens,
    )
