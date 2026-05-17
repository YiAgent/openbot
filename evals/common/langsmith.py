"""LangSmith trace routing for Inspect AI tasks.

The eval pipeline is:

  - Datasets live in **LangSmith** (published by ``evals/scripts/build_*_dataset.py``).
  - Inspect AI orchestrates runs (pulling Examples via
    ``evals.common.datasets.langsmith_dataset``).
  - Run results land in LangSmith as ``ChatAnthropic`` / LangChain traces,
    routed to the single eval project named by ``LANGSMITH_PROJECT_EVAL``.

The prior two-bucket public/internal split was retired — all four v0.1 eval
tasks (``review_martian``, ``chat_swe_qa_pro``, ``fix_swe_bench_verified``,
``test_swt_bench_verified``) now write into one project so cross-cell
comparisons stay trivial.
"""

from __future__ import annotations

import os

from evals.common import config


def configure_tracing_for_dataset(dataset_version: str) -> dict[str, str | None]:
    """Set LangChain / LangSmith env vars so this process's LLM calls auto-trace.

    Idempotent. Reads the single ``LANGSMITH_PROJECT_EVAL`` env var and writes
    it into ``LANGSMITH_PROJECT`` (the var the LangSmith / LangChain SDKs
    actually look at).

    Behavior:
      - Skips entirely (``source="skipped:no-api-key"``) if
        ``LANGSMITH_API_KEY`` is unset — don't crash unit tests / dry runs.
      - Always overwrites a pre-existing ``LANGSMITH_PROJECT``. Doppler's
        default value silently absorbed every eval trace before this rule
        existed, so we never honor a caller override.
      - Sets both ``LANGSMITH_TRACING`` (langsmith ≥0.8) and
        ``LANGCHAIN_TRACING_V2`` (legacy langchain-core readers) when neither
        is already set, so ``langchain_anthropic.ChatAnthropic`` calls inside
        deepagents / the judge auto-publish runs.

    The ``dataset_version`` argument is retained on the signature for caller
    ergonomics (tasks still pass it) and future per-dataset routing, but is
    not currently used.

    Returns the resolved state (for logging / sanity-check).
    """
    del dataset_version  # reserved for future per-dataset routing

    if not os.environ.get("LANGSMITH_API_KEY"):
        return {"project": None, "tracing": None, "source": "skipped:no-api-key"}

    project = os.environ.get(config.LANGSMITH_EVAL_PROJECT_ENV, "")
    if project:
        os.environ["LANGSMITH_PROJECT"] = project

    if "LANGSMITH_TRACING" not in os.environ and "LANGCHAIN_TRACING_V2" not in os.environ:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_TRACING_V2"] = "true"

    tracing = os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING_V2")
    source = "eval-project" if project else "eval-project:unset"
    return {"project": project or None, "tracing": tracing, "source": source}
